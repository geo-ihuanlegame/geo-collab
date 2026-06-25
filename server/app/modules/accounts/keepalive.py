"""账号登录态夜间保活：worker 后台守护线程，窗口内有界随机错峰复用检测按键刷新 storage_state。

设计要点（对齐 tasks/taptap_health.py）：
- 纯函数（窗口判定 / 间隔计算 / 选待刷账号）可单测，不跑真浏览器、不休眠。
- refresh_one_account 复用 check_account(use_browser=True)，带超时看门狗，单账号隔离。
- 后台线程只负责 run_keepalive_once → 按返回的随机 gap 休眠 → 下一轮。
- 仅 worker 进程启动（GEO_ACCOUNT_KEEPALIVE_ENABLED）；web 不启动。
见 docs/superpowers/specs/2026-06-25-account-login-keepalive-design.md
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import threading
from collections.abc import Callable
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select as sa_select

from server.app.core.config import get_settings
from server.app.modules.accounts.models import Account
from server.app.modules.accounts.schemas import AccountCheckRequest
from server.app.shared import feishu

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Any]

_stop = threading.Event()
_thread: threading.Thread | None = None


def parse_hhmm(value: str) -> dt.time:
    hh, mm = value.split(":")
    return dt.time(hour=int(hh), minute=int(mm))


def _to_utc_naive(local_dt: dt.datetime) -> dt.datetime:
    return local_dt.astimezone(dt.UTC).replace(tzinfo=None)


def in_keepalive_window(start: dt.time, end: dt.time, now: dt.datetime) -> bool:
    """now 落在 [start, end] 内（end<start 视为跨午夜）。镜像 pipelines/schedule_calc.in_window。"""
    t = now.timetz().replace(tzinfo=None)
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end  # 跨午夜


def window_start_instant(start: dt.time, now_local: dt.datetime) -> dt.datetime:
    """本窗口起点：<= now 的最近一次 start 出现时刻（今天或昨天），返回 UTC-naive。"""
    candidate = now_local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if candidate > now_local:
        candidate -= dt.timedelta(days=1)
    return _to_utc_naive(candidate)


def window_end_instant(end: dt.time, now_local: dt.datetime) -> dt.datetime:
    """本窗口止点：> now 的最近一次 end 出现时刻（今天或明天），返回 UTC-naive。"""
    candidate = now_local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += dt.timedelta(days=1)
    return _to_utc_naive(candidate)


def compute_next_gap(
    remaining_window_s: float,
    remaining_due: int,
    min_gap: float,
    max_gap: float,
    rng: random.Random,
) -> float:
    """窗口内下一个账号前的随机间隔（从上一个检测完成后计时）。

    cap = 剩余窗口 / 剩余待刷数：账号多→cap 小→上界压缩→当晚刷完；账号少→上界放到 max_gap。
    cap < min_gap（窗口收尾 / 账号过多 / 剩余窗口为负）时退化为恒定 min_gap，连刷。
    """
    cap = max(0.0, remaining_window_s) / max(1, remaining_due)
    hi = min(max_gap, max(min_gap, cap))
    return rng.uniform(min_gap, hi)


def select_due_account_ids(db: Any, window_start: dt.datetime) -> list[int]:
    """本窗口待刷账号 id（最旧 last_checked_at 优先，NULL 最前）。

    入选 = 浏览器账号(state_path 非空) + status='valid' + 未删 + 未合并
         + 本窗口未刷（last_checked_at IS NULL 或 < window_start）。
    """
    rows = (
        db.execute(
            sa_select(Account.id)
            .where(
                Account.state_path.is_not(None),
                Account.status == "valid",
                Account.is_deleted == False,  # noqa: E712
                Account.merged_into.is_(None),
                (Account.last_checked_at.is_(None)) | (Account.last_checked_at < window_start),
            )
            .order_by(Account.last_checked_at.asc())
        )
        .scalars()
        .all()
    )
    return list(rows)


def refresh_one_account(
    session_factory: SessionFactory,
    account_id: int,
    *,
    check_timeout_s: float,
) -> str:
    """复用检测按键刷新单账号登录态，带超时看门狗，单账号隔离，永不抛出。

    返回 refreshed_valid / flipped_expired / lock_busy / timeout / error。
    检测放进内部 daemon 线程，主线程 join(timeout)：超时即放弃该账号、告警、循环继续
    （_run_in_plain_thread 的 join 无超时，这里补上墙钟上界）。
    """
    holder: dict[str, Any] = {}

    def _work() -> None:
        # 在子线程内自建 session：session 非线程安全，所有 DB 操作不跨线程。
        from server.app.modules.accounts.auth import check_account
        from server.app.shared.errors import ClientError

        db = session_factory()
        try:
            account = db.get(Account, account_id)
            if account is None:
                holder["result"] = "error"
                return
            holder["display_name"] = account.display_name or f"#{account_id}"
            before = account.status
            try:
                updated = check_account(db, account, AccountCheckRequest())
                db.commit()
            except ClientError:
                db.rollback()
                holder["result"] = "lock_busy"
                return
            after = getattr(updated, "status", before)
            holder["result"] = (
                "flipped_expired" if before == "valid" and after == "expired" else "refreshed_valid"
            )
        except Exception as exc:  # noqa: BLE001 — 单账号隔离
            try:
                db.rollback()
            except Exception:
                pass
            holder["result"] = "error"
            logger.warning("keepalive refresh #%s failed: %s", account_id, exc)
        finally:
            db.close()

    worker = threading.Thread(target=_work, name=f"keepalive-check-{account_id}", daemon=True)
    worker.start()
    worker.join(timeout=check_timeout_s)

    if worker.is_alive():
        logger.warning("keepalive refresh #%s timed out after %ss", account_id, check_timeout_s)
        feishu.send_text(
            "账号保活检测超时",
            f"账号 #{account_id} 登录态检测超过 {check_timeout_s}s 未完成，已跳过本次保活。",
            level="warning",
        )
        return "timeout"

    result = holder.get("result", "error")
    if result == "flipped_expired":
        feishu.send_text(
            "账号登录态失效",
            f"账号「{holder.get('display_name', f'#{account_id}')}」保活检测发现登录态已失效，请到媒体矩阵重新登录。",
            level="warning",
        )
    return result


def run_keepalive_once(
    session_factory: SessionFactory,
    now_local: dt.datetime,
    rng: random.Random,
) -> dict[str, Any]:
    """一轮保活：不在窗口→跳过；窗口内取最旧待刷账号刷一个，返回下次随机间隔。

    now_local 须带 scheduler_tz 时区。处理一个账号后由调用方休眠 next_gap_seconds
    （从本次检测完成后计时）。
    """
    s = get_settings()
    start = parse_hhmm(s.account_keepalive_window_start)
    end = parse_hhmm(s.account_keepalive_window_end)
    if not in_keepalive_window(start, end, now_local):
        return {"processed": False, "in_window": False}

    win_start = window_start_instant(start, now_local)
    db = session_factory()
    try:
        due = select_due_account_ids(db, win_start)
    finally:
        db.close()

    if not due:
        return {"processed": False, "in_window": True, "remaining_due": 0}

    account_id = due[0]
    result = refresh_one_account(
        session_factory, account_id, check_timeout_s=s.account_keepalive_check_timeout_seconds
    )
    remaining_due = len(due) - 1
    win_end = window_end_instant(end, now_local)
    remaining_window_s = (win_end - _to_utc_naive(now_local)).total_seconds()
    gap = compute_next_gap(
        remaining_window_s,
        remaining_due,
        s.account_keepalive_min_gap_seconds,
        s.account_keepalive_max_gap_seconds,
        rng,
    )
    return {
        "processed": True,
        "in_window": True,
        "account_id": account_id,
        "result": result,
        "remaining_due": remaining_due,
        "next_gap_seconds": gap,
    }


def start_keepalive(session_factory: SessionFactory) -> bool:
    """按配置启动后台保活线程。返回是否启动（关闭 / 已在跑 → False）。"""
    global _thread
    if not get_settings().account_keepalive_enabled:
        return False
    if _thread is not None and _thread.is_alive():
        return False

    _stop.clear()
    rng = random.Random()

    def _loop() -> None:
        while not _stop.is_set():
            tz = ZoneInfo(get_settings().scheduler_tz)
            try:
                r = run_keepalive_once(session_factory, dt.datetime.now(tz), rng)
            except Exception:
                logger.exception("account keepalive round failed")
                r = {"processed": False}
            if r.get("processed"):
                sleep_s = float(r.get("next_gap_seconds") or 0.0)
            else:
                sleep_s = float(get_settings().account_keepalive_poll_seconds)
            if _stop.wait(max(1.0, sleep_s)):
                break

    _thread = threading.Thread(target=_loop, daemon=True, name="account-keepalive")
    _thread.start()
    return True


def stop_keepalive() -> None:
    """请求停止后台线程（worker 优雅关闭 / 测试用）。"""
    _stop.set()
