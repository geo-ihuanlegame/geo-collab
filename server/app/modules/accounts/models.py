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
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.app.core.time import utcnow
from server.app.db.base import Base


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "platform_id", "platform_user_id", name="uq_accounts_platform_user"
        ),
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
    )  # valid / expired / unknown
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    state_path: Mapped[str] = mapped_column(
        String(1000)
    )  # Playwright storage_state.json 的相对路径
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    platform = relationship("Platform", back_populates="accounts")
    publish_task_accounts = relationship("PublishTaskAccount", back_populates="account")
    publish_records = relationship("PublishRecord", back_populates="account")


class AccountLoginSession(Base):
    """Worker-owned interactive account login session command/state row."""

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
    """Cross-process browser session registry — written by worker, read by API."""

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
    """Maps a publish record to the browser session handling it."""

    __tablename__ = "record_browser_sessions"

    record_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("publish_records.id", ondelete="CASCADE"), primary_key=True
    )
    session_id: Mapped[str] = mapped_column(
        String(12), ForeignKey("browser_sessions.id", ondelete="CASCADE"), nullable=False
    )


class BrowserProfileLock(Base):
    """Cross-process lock for one Chrome persistent profile directory."""

    __tablename__ = "browser_profile_locks"

    profile_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    owner_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(80), nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    queue_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    lease_until: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
