"""账号模块的 Pydantic 请求 / 响应模型，以及 ORM Account → AccountRead 的序列化函数。"""

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from server.app.modules.accounts.models import Account


# ── 响应体 ──────────────────────────────────────────────────────────────────


class AccountRead(BaseModel):
    id: int
    platform_code: str
    platform_name: str
    display_name: str
    platform_user_id: str | None
    status: str  # valid / expired / unknown
    last_checked_at: datetime | None
    last_login_at: datetime | None
    state_path: str  # Playwright storage_state.json 路径
    note: str | None
    created_at: datetime
    updated_at: datetime


class AccountBrowserSessionRead(BaseModel):
    account: AccountRead
    platform_code: str
    account_key: str
    session_id: str
    novnc_url: str | None = None
    status: str | None = None
    queue_reason: str | None = None


class AccountBrowserSessionFinishRead(BaseModel):
    account: AccountRead
    logged_in: bool
    url: str
    title: str


class LoginSessionStatusRead(BaseModel):
    status: str
    novnc_url: str | None = None
    error_message: str | None = None
    queue_reason: str | None = None
    browser_session_id: str | None = None


# ── 请求体 ──────────────────────────────────────────────────────────────────


class PlatformLoginRequest(BaseModel):
    display_name: str = Field(default="头条号账号", min_length=1, max_length=200)
    account_key: str | None = Field(default=None, max_length=120)  # 本地存储目录标识
    channel: str = "chromium"
    executable_path: str | None = None
    wait_seconds: int = Field(default=180, ge=5, le=600)  # 等待登录完成的超时时间（秒）
    use_browser: bool = True  # True=打开浏览器交互登录，False=复用已有状态
    note: str | None = None


class AccountCheckRequest(BaseModel):
    channel: str = "chromium"
    executable_path: str | None = None
    use_browser: bool = True


class AccountRenameRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)


class AccountExportRequest(BaseModel):
    account_ids: list[int] | None = None  # 为空则导出所有


# ── 序列化函数（原 api/serializers.py:to_account_read）───────────────────────


def to_account_read(account: "Account") -> AccountRead:
    return AccountRead(
        id=account.id,
        platform_code=account.platform.code,
        platform_name=account.platform.name,
        display_name=account.display_name,
        platform_user_id=account.platform_user_id,
        status=account.status,
        last_checked_at=account.last_checked_at,
        last_login_at=account.last_login_at,
        state_path=account.state_path,
        note=account.note,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )
