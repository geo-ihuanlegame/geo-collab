from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import delete as sa_delete, select
from sqlalchemy.orm import Session, selectinload

from server.app.core.paths import get_data_dir
from server.app.core.time import utcnow
from server.app.models import Account, Platform, PublishRecord, PublishTaskAccount, TaskLog
from server.app.shared.errors import ClientError


def normalize_account_key(account_key: str | None) -> str:
    raw = account_key or uuid.uuid4().hex
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-")
    return value or uuid.uuid4().hex


def state_dir_for_key(platform_code: str, account_key: str) -> Path:
    return get_data_dir() / "browser_states" / platform_code / account_key


def state_path_for_key(platform_code: str, account_key: str) -> Path:
    return state_dir_for_key(platform_code, account_key) / "storage_state.json"


def profile_dir_for_key(platform_code: str, account_key: str) -> Path:
    return state_dir_for_key(platform_code, account_key) / "profile"


def relative_to_data_dir(path: Path) -> str:
    return path.resolve().relative_to(get_data_dir().resolve()).as_posix()


def account_key_from_state_path(state_path: str) -> tuple[str, str]:
    parts = Path(state_path).parts
    try:
        idx = parts.index("browser_states")
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
    stmt = select(Account).options(selectinload(Account.platform)).order_by(Account.updated_at.desc())
    return list(db.execute(stmt).scalars().all())


def get_account(db: Session, account_id: int) -> Account | None:
    stmt = select(Account).where(Account.id == account_id).options(selectinload(Account.platform))
    return db.execute(stmt).scalar_one_or_none()


def rename_account(db: Session, account: Account, display_name: str) -> Account:
    account.display_name = display_name.strip()
    account.updated_at = utcnow()
    db.flush()
    return get_account(db, account.id) or account


def delete_account(db: Session, account: Account) -> None:
    account_id = account.id

    active = db.execute(
        select(PublishRecord.id).where(
            PublishRecord.account_id == account_id,
            PublishRecord.status.in_(["pending", "running", "waiting_manual_publish", "waiting_user_input"]),
        )
    ).scalars().all()
    if active:
        raise ClientError("存在未完成发布记录，无法删除账号")

    db.execute(sa_delete(PublishTaskAccount).where(PublishTaskAccount.account_id == account_id))
    record_ids = list(
        db.execute(select(PublishRecord.id).where(PublishRecord.account_id == account_id)).scalars()
    )
    if record_ids:
        db.execute(sa_delete(TaskLog).where(TaskLog.record_id.in_(record_ids)))
        db.execute(sa_delete(PublishRecord).where(PublishRecord.id.in_(record_ids)))
    db.delete(account)
    db.flush()


def _get_driver(platform_code: str):
    from server.app.modules.tasks.drivers import get_driver
    return get_driver(platform_code)
