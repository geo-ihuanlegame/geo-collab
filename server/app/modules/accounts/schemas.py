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
    status: str  # 状态：valid / expired / unknown
    last_checked_at: datetime | None
    last_login_at: datetime | None
    state_path: str | None  # Playwright storage_state.json 路径；API 型账号为 None
    note: str | None
    contact: str | None = None  # 绑定联系方式
    avatar_asset_id: str | None = None
    distribution_enabled: bool = True
    app_id: str | None = None  # API 型账号的 AppID（明文）；TapTap = 游戏 app_id
    app_secret_tail: str | None = None  # AppSecret 尾 4 位掩码；原文永不回传
    group_id: str | None = None  # TapTap 论坛版块 id（明文）
    x_ua_configured: bool = False  # TapTap 是否已显式配置 x_ua（原文不回传，仅回是否已设）
    # ── 共享账号派生字段（见设计稿 §2.6）──────────────────────────────────
    owner_name: str | None = None  # owner 的显示名（display_name 优先回落 username）
    member_count: int = 0  # 该账号被授予的成员数（不含 owner）
    can_manage: bool = False  # 请求方能否管理（= user_can_manage_account：admin 或 owner）
    identity_known: bool = (
        False  # platform_user_id 非空（头条 creator-ID / 公众号 AppID）；供「身份未知」徽标
    )
    created_at: datetime
    updated_at: datetime


class AccountMemberRead(BaseModel):
    """共享账号成员行（含 owner 标识 + 溯源）。供后续成员管理端点用。"""

    account_id: int
    user_id: int
    username: str | None = None
    display_name: str | None = None
    is_owner: bool = False
    granted_via: str | None = None  # owner 行无 granted_via
    created_at: datetime | None = None


class BackfillIdentityResult(BaseModel):
    """admin 批量回填 creator-ID 的汇总计数。供后续批量回填端点用。"""

    processed: int = 0
    backfilled: int = 0
    merged: int = 0
    conflicts: int = 0
    still_unknown: int = 0
    failed: int = 0


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
    # 查重决议后的 canonical 账号 id（共享账号，见设计稿 §4）：worker finish 后非空时，
    # 可能 != 发起登录的账号 id，前端据此跳到共享 canonical。
    resolved_account_id: int | None = None


# ── 请求体 ──────────────────────────────────────────────────────────────────


class PlatformLoginRequest(BaseModel):
    display_name: str = Field(default="头条号账号", min_length=1, max_length=200)
    account_key: str | None = Field(default=None, max_length=120)  # 本地存储目录标识
    channel: str = "chromium"
    executable_path: str | None = None
    wait_seconds: int = Field(default=180, ge=5, le=600)  # 等待登录完成的超时时间（秒）
    use_browser: bool = True  # 为 True 时打开浏览器交互登录；为 False 时复用已有状态
    note: str | None = None
    # ── 通用账号字段（与 ApiAccountCreate 对齐）：浏览器平台建号时由「添加账号」弹窗一并填入 ──
    contact: str | None = Field(default=None, max_length=200)  # 绑定联系方式
    avatar_asset_id: str | None = Field(default=None, max_length=64)  # 账号头像
    distribution_enabled: bool = True  # 分发开关


class AccountCheckRequest(BaseModel):
    channel: str = "chromium"
    executable_path: str | None = None
    use_browser: bool = True


class ApiCredentialsIn(BaseModel):
    app_id: str = Field(min_length=1, max_length=100)
    app_secret: str = Field(min_length=1, max_length=200)


class TaptapForumIn(BaseModel):
    """TapTap 账号论坛绑定配置（每账号固定一个论坛；x_ua 选填，留空则由 VID 合成）。"""

    app_id: str = Field(min_length=1, max_length=100)  # 游戏 app_id
    group_id: str = Field(min_length=1, max_length=100)  # 论坛版块 id
    x_ua: str | None = Field(default=None, max_length=500)  # 选填，捕获的 X-UA 原串


class ApiAccountCreate(BaseModel):
    """API 型平台（如微信公众号）账号创建：凭据直填，无浏览器登录。"""

    platform_code: str = Field(min_length=1, max_length=50)
    display_name: str = Field(min_length=1, max_length=200)
    api_credentials: ApiCredentialsIn
    contact: str | None = Field(default=None, max_length=200)
    note: str | None = None
    avatar_asset_id: str | None = Field(default=None, max_length=64)
    distribution_enabled: bool = True


class AccountUpdateRequest(BaseModel):
    """账号通用 PATCH：全部可选，未传字段不动；api_credentials 传则整体替换。"""

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    contact: str | None = Field(default=None, max_length=200)
    note: str | None = None
    avatar_asset_id: str | None = Field(default=None, max_length=64)
    distribution_enabled: bool | None = None
    api_credentials: ApiCredentialsIn | None = None


class AccountExportRequest(BaseModel):
    account_ids: list[int] | None = None  # 为空则导出所有


# ── 序列化函数（原 api/serializers.py:to_account_read）───────────────────────


def to_account_read(
    account: "Account",
    *,
    owner_name: str | None = None,
    member_count: int = 0,
    can_manage: bool = False,
) -> AccountRead:
    """ORM Account → AccountRead。

    共享账号派生字段（owner_name / member_count / can_manage）由调用方计算后注入（需要 viewer
    身份 + 成员数查询，见设计稿 §2.6）；缺省时取保守默认（can_manage=False、member_count=0）。
    identity_known 纯由账号自身派生：platform_user_id 非空即已知（头条=creator-ID 已抽取 /
    公众号=AppID，两类平台共用此字段），与 viewer 无关，故不由调用方传入。
    """
    creds = account.api_credentials or {}
    secret = creds.get("app_secret") or ""
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
        contact=account.contact,
        avatar_asset_id=account.avatar_asset_id,
        distribution_enabled=account.distribution_enabled,
        app_id=creds.get("app_id"),
        app_secret_tail=secret[-4:] if secret else None,
        group_id=creds.get("group_id"),
        x_ua_configured=bool(creds.get("x_ua")),
        owner_name=owner_name,
        member_count=member_count,
        can_manage=can_manage,
        identity_known=bool(account.platform_user_id),
        created_at=account.created_at,
        updated_at=account.updated_at,
    )
