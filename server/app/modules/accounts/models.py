"""
账号模块 ORM 模型。

包含：
  - Account             — 平台媒体账号
  - AccountLoginSession — 交互式登录会话（Worker 驱动）
  - BrowserSession      — 跨进程浏览器会话注册表
  - RecordBrowserSession— 发布记录 → 浏览器会话映射
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.app.core.time import utcnow
from server.app.db.base import Base


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("platform_id", "platform_user_id", name="uq_accounts_platform_user"),
        CheckConstraint("status in ('valid', 'expired', 'unknown')", name="ck_accounts_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey("platforms.id"), index=True)
    display_name: Mapped[str] = mapped_column(String(200))  # 用户自定义显示名称
    platform_user_id: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )  # 平台侧用户 ID
    status: Mapped[str] = mapped_column(
        String(30), default="unknown", index=True
    )  # 状态：valid / expired / unknown
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    state_path: Mapped[str | None] = mapped_column(
        String(1000), nullable=True
    )  # Playwright storage_state.json 的相对路径；API 型账号（如公众号）为 NULL
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ── API 型平台（公众号等）专用 ───────────────────────────────────────
    api_credentials: Mapped[dict | None] = mapped_column(
        MutableDict.as_mutable(JSON), nullable=True
    )  # {"app_id": ..., "app_secret": ...}；永不通过 API 回传原文
    api_token_cache: Mapped[dict | None] = mapped_column(
        MutableDict.as_mutable(JSON), nullable=True
    )  # {"access_token": ..., "expires_at": <epoch秒>}；web/worker 跨进程共享
    # ── 通用账号字段（对齐媒体矩阵交互稿）─────────────────────────────────
    distribution_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", index=True
    )  # 分发开关：False 时 pipeline distribute 自动派号跳过该账号
    contact: Mapped[str | None] = mapped_column(String(200), nullable=True)  # 绑定联系方式
    avatar_asset_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("assets.id"), nullable=True
    )  # 账号头像
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    platform = relationship("Platform", back_populates="accounts")
    publish_task_accounts = relationship("PublishTaskAccount", back_populates="account")
    publish_records = relationship("PublishRecord", back_populates="account")


class AccountLoginSession(Base):
    """Worker 拥有的交互式账号登录会话命令 / 状态记录。"""

    __tablename__ = "account_login_sessions"

    id: Mapped[str] = mapped_column(String(12), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    platform_code: Mapped[str] = mapped_column(String(80), nullable=False)
    account_key: Mapped[str] = mapped_column(String(200), nullable=False)
    channel: Mapped[str] = mapped_column(String(80), nullable=False, default="chromium")
    executable_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    browser_session_id: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    novnc_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    logged_in: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    result_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    result_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    queue_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    previous_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BrowserSession(Base):
    """跨进程浏览器会话注册表：由 worker 写入，API 读取。"""

    __tablename__ = "browser_sessions"

    id: Mapped[str] = mapped_column(String(12), primary_key=True)
    platform_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    account_key: Mapped[str] = mapped_column(String(200), nullable=False)
    profile_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display: Mapped[str | None] = mapped_column(String(20), nullable=True)
    novnc_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    keep_alive: Mapped[bool] = mapped_column(Boolean, default=False)
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False)


class RecordBrowserSession(Base):
    """发布记录到处理它的浏览器会话的映射。"""

    __tablename__ = "record_browser_sessions"

    record_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("publish_records.id", ondelete="CASCADE"), primary_key=True
    )
    session_id: Mapped[str] = mapped_column(
        String(12), ForeignKey("browser_sessions.id", ondelete="CASCADE"), nullable=False
    )


class BrowserProfileLock(Base):
    """单个 Chrome 持久化 profile 目录的跨进程锁。"""

    __tablename__ = "browser_profile_locks"

    profile_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    # owner_kind 取值：publish / login / account_check
    owner_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(80), nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    queue_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # 租约到期时间：过期锁会被下一个 try_acquire_profile_lock 直接删掉抢占（防 owner 崩溃后死锁）
    lease_until: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
