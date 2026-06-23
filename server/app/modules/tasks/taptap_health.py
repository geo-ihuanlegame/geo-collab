"""TapTap 账号 cookie 体检：应用内后台守护线程，纯 HTTP 探测 account-profile/v1/me。

设计要点（对齐 ai_generation/sync_scheduler）：
- `check_account_cookie(state, forum, transport=)` 是纯函数式单账号探测，**可单测**（MockTransport）。
- `run_cookie_check_once(session_factory)` 扫所有 taptap 账号探一轮：明确鉴权失效(401/未登录)→
  置 status='expired' 并攒进飞书告警；瞬时错误(网络等)只记日志、不翻状态（避免误报）。
- 后台线程只负责 `wait(interval) → run_cookie_check_once` 循环；create_app() 在
  GEO_TAPTAP_COOKIE_CHECK_ENABLED=true 时启动。**不做自动登录**（SMS 验证码躲不开），只告警喊人重登。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from server.app.core.config import get_settings
from server.app.core.paths import get_data_dir
from server.app.core.time import utcnow
from server.app.modules.accounts.models import Account
from server.app.modules.accounts.secret_files import read_state
from server.app.modules.system.models import Platform
from server.app.modules.tasks.drivers.taptap_client import (
    TapTapApiError,
    TapTapAuthError,
    build_x_ua,
    get_me,
    make_client,
)
from server.app.shared import feishu

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Any]

_check_thread: threading.Thread | None = None
_check_stop = threading.Event()


@dataclass
class CookieCheckResult:
    ok: bool  # cookie 有效
    expired: bool  # 明确鉴权失效（需重登 + 告警）；瞬时错误时为 False
    vid: str | None  # 探测顺带拿到的用户 id
    message: str


def check_account_cookie(
    state: dict[str, Any] | None,
    forum: dict[str, Any] | None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> CookieCheckResult:
    """探测单个 taptap 账号 cookie 是否有效。transport 仅供测试注入。"""
    forum = forum or {}
    app_id, group_id, x_ua = forum.get("app_id"), forum.get("group_id"), forum.get("x_ua")
    if not state:
        return CookieCheckResult(False, True, None, "缺登录态（storage_state）")
    if not x_ua:
        return CookieCheckResult(False, False, None, "未配置 x_ua（论坛配置不全），跳过探测")
    try:
        client = make_client(state, app_id=app_id or 0, group_id=group_id or 0, transport=transport)
    except TapTapAuthError as exc:
        return CookieCheckResult(False, True, None, str(exc))
    try:
        data = get_me(client, x_ua=x_ua)
        vid = data.get("id")
        return CookieCheckResult(True, False, str(vid) if vid is not None else None, "ok")
    except TapTapAuthError as exc:
        return CookieCheckResult(False, True, None, str(exc))
    except (TapTapApiError, httpx.HTTPError) as exc:  # 瞬时错误：不翻状态
        return CookieCheckResult(False, False, None, f"探测异常（瞬时）: {exc}")
    finally:
        client.close()


def _taptap_account_ids(db: Any) -> list[int]:
    return [
        row[0]
        for row in db.query(Account.id)
        .join(Platform, Account.platform_id == Platform.id)
        .filter(
            Platform.code == "taptap",
            Account.is_deleted == False,  # noqa: E712
            Account.merged_into.is_(None),
            Account.state_path.is_not(None),
        )
        .all()
    ]


def run_cookie_check_once(session_factory: SessionFactory) -> dict[str, int]:
    """扫所有已登录过的 taptap 账号探一轮。返回 {checked, valid, expired, errors}。

    expired 的账号会被攒成一条飞书告警（一轮一条，不逐账号刷屏）。
    """
    db = session_factory()
    try:
        account_ids = _taptap_account_ids(db)
    finally:
        db.close()

    valid = expired = errors = 0
    expired_names: list[str] = []
    for account_id in account_ids:
        db = session_factory()
        try:
            account = db.get(Account, account_id)
            if account is None or not account.state_path:
                continue
            abs_state = get_data_dir() / account.state_path
            state = read_state(abs_state) if abs_state.exists() else None
            forum = dict(account.api_credentials or {})
            if not forum.get("x_ua") and account.platform_user_id:
                forum["x_ua"] = build_x_ua(account.platform_user_id)
            result = check_account_cookie(state, forum)
            account.last_checked_at = utcnow()
            if result.ok:
                valid += 1
                account.status = "valid"
                if result.vid and not account.platform_user_id:
                    account.platform_user_id = result.vid
            elif result.expired:
                expired += 1
                account.status = "expired"
                expired_names.append(account.display_name or f"#{account.id}")
            else:
                errors += 1
                logger.info("taptap cookie check transient for #%s: %s", account_id, result.message)
            db.commit()
        except Exception as exc:  # noqa: BLE001 — 单账号失败隔离
            db.rollback()
            errors += 1
            logger.warning("taptap cookie check failed for #%s: %s", account_id, exc)
        finally:
            db.close()

    if expired_names:
        feishu.send_text(
            "TapTap 登录态失效",
            "以下账号 cookie 已失效，请到媒体矩阵重新登录：\n"
            + "\n".join(f"· {n}" for n in expired_names),
            level="warning",
        )
    return {"checked": len(account_ids), "valid": valid, "expired": expired, "errors": errors}


def start_cookie_check(session_factory: SessionFactory) -> bool:
    """按配置启动后台体检线程。返回是否启动（关闭或已在运行返回 False）。"""
    global _check_thread
    if not get_settings().taptap_cookie_check_enabled:
        return False
    if _check_thread is not None and _check_thread.is_alive():
        return False

    _check_stop.clear()

    def _loop() -> None:
        while not _check_stop.is_set():
            interval = max(300, get_settings().taptap_cookie_check_interval_seconds)
            if _check_stop.wait(interval):  # 先等再探：避免一启动就打飞书
                break
            try:
                result = run_cookie_check_once(session_factory)
                logger.info("taptap cookie check round: %s", result)
            except Exception:
                logger.exception("taptap cookie check round failed")

    _check_thread = threading.Thread(target=_loop, daemon=True, name="taptap-cookie-check")
    _check_thread.start()
    return True


def stop_cookie_check() -> None:
    _check_stop.set()
