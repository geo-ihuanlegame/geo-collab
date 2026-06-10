"""账号增删改查与本地存储路径换算。

核心是一组在 platform_code/account_key 与 data 目录下相对路径之间互转的纯函数：
storage_state.json（Playwright 登录态）、profile（Chromium 持久化目录）、
profile_key（跨进程 profile 锁的键）都由这里统一推导，确保 worker 与 API 端算法一致。
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from server.app.core.paths import get_data_dir
from server.app.core.time import utcnow
from server.app.modules.accounts.models import Account
from server.app.modules.accounts.schemas import AccountUpdateRequest, ApiAccountCreate
from server.app.modules.system.models import Platform
from server.app.modules.tasks.drivers.wechat_client import (
    TOKEN_REFRESH_SKEW_SECONDS,
    WeChatApiError,
    make_default_client,
)
from server.app.modules.tasks.drivers.wechat_client import (
    fetch_access_token as wechat_fetch_access_token,
)
from server.app.modules.tasks.models import PublishRecord
from server.app.shared.errors import ClientError, ConflictError, ValidationError

_API_PLATFORM_LABELS = {"wechat_mp": "微信公众号"}
_API_PLATFORM_CODES = set(_API_PLATFORM_LABELS)


def normalize_account_key(account_key: str | None) -> str:
    """把账号标识收敛成只含字母数字 _-的安全目录名；为空 / 清洗后为空则随机生成一个。"""
    raw = account_key or uuid.uuid4().hex
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-")
    return value or uuid.uuid4().hex


def state_dir_for_key(platform_code: str, account_key: str, user_id: int | None = None) -> Path:
    if user_id is None:
        return get_data_dir() / "browser_states" / platform_code / account_key
    return get_data_dir() / "browser_states" / "users" / str(user_id) / platform_code / account_key


def state_path_for_key(platform_code: str, account_key: str, user_id: int | None = None) -> Path:
    return state_dir_for_key(platform_code, account_key, user_id=user_id) / "storage_state.json"


def profile_dir_for_key(platform_code: str, account_key: str, user_id: int | None = None) -> Path:
    return state_dir_for_key(platform_code, account_key, user_id=user_id) / "profile"


def state_dir_from_state_path(state_path: str) -> Path:
    return get_data_dir() / Path(state_path).parent


def profile_dir_from_state_path(state_path: str) -> Path:
    return state_dir_from_state_path(state_path) / "profile"


def profile_key_from_state_path(state_path: str) -> str:
    """从 state_path 推出 profile 锁的键（即 storage_state.json 所在目录的相对路径）。

    超过列宽（profile_key 列 255）时退化成 sha256 摘要，保证能落进 DB 主键列。
    """
    key = Path(state_path).parent.as_posix()
    if len(key) <= 240:
        return key
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def clear_profile_locks(profile_dir: Path) -> None:
    """删掉 Chromium 留在持久化 profile 目录里的 Singleton* 残锁。

    上次浏览器进程非正常退出会遗留这些文件，导致下次 launch_persistent_context 拒绝启动。
    """
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock = profile_dir / name
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


def relative_to_data_dir(path: Path) -> str:
    return path.resolve().relative_to(get_data_dir().resolve()).as_posix()


def account_key_from_state_path(state_path: str) -> tuple[str, str]:
    """从 state_path 反解出 (platform_code, account_key)，是 state_path_for_key 的逆运算。

    兼容两种布局：新版带用户隔离 browser_states/users/<uid>/<platform>/<key>/...，
    旧版无用户层 browser_states/<platform>/<key>/...（由 idx+1 是否为 "users" 区分）。
    """
    parts = Path(state_path).parts
    try:
        idx = parts.index("browser_states")
        if parts[idx + 1] == "users":
            return parts[idx + 3], parts[idx + 4]
        return parts[idx + 1], parts[idx + 2]
    except (ValueError, IndexError):
        raise ClientError(f"Invalid state path: {state_path}") from None


def get_or_create_platform(db: Session, code: str, name: str, base_url: str | None) -> Platform:
    platform = db.execute(select(Platform).where(Platform.code == code)).scalar_one_or_none()
    if platform is not None:
        return platform

    platform = Platform(code=code, name=name, base_url=base_url, enabled=True)
    db.add(platform)
    db.flush()
    db.refresh(platform)
    return platform


def launch_options(channel: str, executable_path: str | None) -> dict[str, Any]:
    options: dict[str, Any] = {
        "headless": False,
        "viewport": {"width": 1440, "height": 900},
        "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    }
    if channel and channel.lower() != "chromium":
        options["channel"] = channel
    if executable_path:
        path = Path(executable_path)
        if not path.is_absolute():
            raise ClientError(f"Executable path must be absolute: {executable_path}")
        if not path.is_file():
            raise ClientError(f"Executable not found: {executable_path}")
        options["executable_path"] = executable_path
    return options


def list_accounts(db: Session) -> list[Account]:
    stmt = (
        select(Account)
        .where(Account.is_deleted == False)  # noqa: E712
        .options(selectinload(Account.platform))
        .order_by(Account.updated_at.desc())
    )
    return list(db.execute(stmt).scalars().all())


def get_account(db: Session, account_id: int) -> Account | None:
    stmt = (
        select(Account)
        .where(Account.id == account_id, Account.is_deleted == False)  # noqa: E712
        .options(selectinload(Account.platform))
    )
    return db.execute(stmt).scalar_one_or_none()


def is_api_platform_code(code: str) -> bool:
    from server.app.modules.tasks.drivers import is_api_driver

    return code in _API_PLATFORM_CODES or is_api_driver(code)


def api_platform_options() -> list[dict[str, str]]:
    return [
        {"code": code, "name": name}
        for code, name in sorted(_API_PLATFORM_LABELS.items(), key=lambda item: item[0])
    ]


def create_api_account(db: Session, user_id: int, payload: ApiAccountCreate) -> Account:
    """创建 API 型平台账号：凭据直存，platform_user_id 取 AppID 防重复登记。"""
    if not is_api_platform_code(payload.platform_code):
        raise ValidationError(f"平台 {payload.platform_code} 为浏览器登录接入，请走扫码授权流程")

    platform = db.execute(
        select(Platform).where(Platform.code == payload.platform_code)
    ).scalar_one_or_none()
    if platform is None:
        raise ValidationError(f"平台不存在: {payload.platform_code}")

    app_id = payload.api_credentials.app_id
    duplicate = db.execute(
        select(Account).where(
            Account.user_id == user_id,
            Account.platform_id == platform.id,
            Account.platform_user_id == app_id,
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ConflictError(f"该 AppID 已登记: {app_id}")

    account = Account(
        user_id=user_id,
        platform=platform,
        display_name=payload.display_name.strip(),
        platform_user_id=app_id,
        status="unknown",
        state_path=None,
        api_credentials={
            "app_id": app_id,
            "app_secret": payload.api_credentials.app_secret,
        },
        contact=payload.contact,
        note=payload.note,
        avatar_asset_id=payload.avatar_asset_id,
        distribution_enabled=payload.distribution_enabled,
    )
    db.add(account)
    db.flush()
    return get_account(db, account.id) or account


def _ensure_app_id_available(db: Session, account: Account, app_id: str) -> None:
    duplicate = db.execute(
        select(Account.id).where(
            Account.user_id == account.user_id,
            Account.platform_id == account.platform_id,
            Account.platform_user_id == app_id,
            Account.id != account.id,
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ConflictError(f"该 AppID 已登记: {app_id}")


def update_account_fields(db: Session, account: Account, payload: AccountUpdateRequest) -> Account:
    """通用 PATCH：仅更新显式传入的非 None 字段；api_credentials 整体替换。"""
    data = payload.model_dump(exclude_unset=True)
    if data.get("display_name") is not None:
        account.display_name = data["display_name"].strip()
    if data.get("contact") is not None:
        account.contact = data["contact"]
    if data.get("note") is not None:
        account.note = data["note"]
    if data.get("avatar_asset_id") is not None:
        account.avatar_asset_id = data["avatar_asset_id"]
    if data.get("distribution_enabled") is not None:
        account.distribution_enabled = data["distribution_enabled"]
    if data.get("api_credentials") is not None:
        if not is_api_platform_code(account.platform.code):
            raise ValidationError("浏览器登录平台不支持 API 凭据")
        assert payload.api_credentials is not None  # type guard for mypy
        creds = payload.api_credentials
        _ensure_app_id_available(db, account, creds.app_id)
        account.api_credentials = {"app_id": creds.app_id, "app_secret": creds.app_secret}
        account.platform_user_id = creds.app_id
        account.api_token_cache = None
        account.status = "unknown"
        account.last_checked_at = None
    account.updated_at = utcnow()
    db.flush()
    return get_account(db, account.id) or account


def verify_api_credentials(db: Session, account: Account) -> Account:
    """强制刷新 access_token 验证凭据；失败时置 expired 并透传微信错误。"""
    if not is_api_platform_code(account.platform.code):
        raise ValidationError("该平台为浏览器登录接入，无凭据可验证")
    creds = account.api_credentials or {}
    if not creds.get("app_id") or not creds.get("app_secret"):
        raise ValidationError("账号未配置 AppID/AppSecret")

    client = make_default_client()
    try:
        token, expires_in = wechat_fetch_access_token(
            creds["app_id"], creds["app_secret"], client=client
        )
    except WeChatApiError as exc:
        account.status = "expired"
        account.api_token_cache = None
        account.last_checked_at = utcnow()
        account.updated_at = account.last_checked_at
        db.flush()
        raise ValidationError(str(exc)) from exc
    finally:
        client.close()

    account.api_token_cache = {
        "access_token": token,
        "expires_at": int(time.time()) + int(expires_in),
    }
    account.status = "valid"
    account.last_checked_at = utcnow()
    account.updated_at = account.last_checked_at
    db.flush()
    return get_account(db, account.id) or account


def get_cached_wechat_token(account: Account) -> str | None:
    if account.status != "valid":
        return None
    cache = account.api_token_cache or {}
    token = cache.get("access_token")
    expires_at = int(cache.get("expires_at") or 0)
    if not token or expires_at <= int(time.time()) + TOKEN_REFRESH_SKEW_SECONDS:
        return None
    return str(token)


def rename_account(db: Session, account: Account, display_name: str) -> Account:
    account.display_name = display_name.strip()
    account.updated_at = utcnow()
    db.flush()
    return get_account(db, account.id) or account


def delete_account(db: Session, account: Account) -> None:
    """软删账号（置 is_deleted）。仍有未完成发布记录时抛 ClientError 拒绝删除。"""
    account_id = account.id

    active = (
        db.execute(
            select(PublishRecord.id).where(
                PublishRecord.account_id == account_id,
                PublishRecord.status.in_(
                    ["pending", "running", "waiting_manual_publish", "waiting_user_input"]
                ),
            )
        )
        .scalars()
        .all()
    )
    if active:
        raise ClientError("存在未完成发布记录，无法删除账号")

    account.is_deleted = True
    account.deleted_at = utcnow()
    account.updated_at = utcnow()
    db.flush()


def _get_driver(platform_code: str):
    from server.app.modules.tasks.drivers import get_driver

    return get_driver(platform_code)
