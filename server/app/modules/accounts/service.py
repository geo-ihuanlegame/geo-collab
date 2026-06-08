"""账号 CRUD 与本地存储路径换算。

核心是一组在 platform_code/account_key 与 data 目录下相对路径之间互转的纯函数：
storage_state.json（Playwright 登录态）、profile（Chromium 持久化目录）、
profile_key（跨进程 profile 锁的键）都由这里统一推导，确保 worker 与 API 端算法一致。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from server.app.core.paths import get_data_dir
from server.app.core.time import utcnow
from server.app.modules.accounts.models import Account
from server.app.modules.system.models import Platform
from server.app.modules.tasks.models import PublishRecord
from server.app.shared.errors import ClientError


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
