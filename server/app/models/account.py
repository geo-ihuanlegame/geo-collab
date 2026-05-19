from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.app.core.time import utcnow
from server.app.db.base import Base


# 平台账号：每个账号关联一个 Playwright 浏览器存储状态
class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("user_id", "platform_id", "platform_user_id", name="uq_accounts_platform_user"),
        CheckConstraint("status in ('valid', 'expired', 'unknown')", name="ck_accounts_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey("platforms.id"), index=True)
    display_name: Mapped[str] = mapped_column(String(200))  # 用户自定义显示名称
    platform_user_id: Mapped[str | None] = mapped_column(String(200), nullable=True)  # 平台侧用户 ID
    status: Mapped[str] = mapped_column(String(30), default="unknown", index=True)  # valid / expired / unknown
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    state_path: Mapped[str] = mapped_column(String(1000))  # Playwright storage_state.json 的相对路径
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    platform = relationship("Platform", back_populates="accounts")
    publish_task_accounts = relationship("PublishTaskAccount", back_populates="account")
    publish_records = relationship("PublishRecord", back_populates="account")
