"""
发布任务核心模型：PublishTask → PublishRecord → TaskLog。

状态机概览：

  PublishTask:
    pending → running → succeeded
                      → partial_failed
                      → failed
                      → cancelled (any state)

  PublishRecord:
    pending → running → succeeded
                      → failed
                      → cancelled
                      → waiting_manual_publish (stop_before_publish 模式)
                      → waiting_user_input (扫码/验证码等人工介入)

  关联方式：
    Task 1 ──→ N Records (每篇文章 × 每个账号 = 一条 Record)
    Task 1 ──→ N Logs
    Record ──→ N Logs (可附带失败截图 asset_id)
"""
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.app.core.time import utcnow
from server.app.db.base import Base


# 发布任务：单篇或分组轮询，关联多个账号，包含多条发布记录
class PublishTask(Base):
    __tablename__ = "publish_tasks"
    __table_args__ = (
        CheckConstraint("task_type in ('single', 'group_round_robin')", name="ck_publish_tasks_task_type"),
        CheckConstraint(
            "status in ('pending', 'running', 'succeeded', 'partial_failed', 'failed', 'cancelled')",
            name="ck_publish_tasks_status",
        ),
        UniqueConstraint("client_request_id", name="uq_publish_tasks_client_request_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    task_type: Mapped[str] = mapped_column(String(40), index=True)  # single / group_round_robin
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    platform_id: Mapped[int | None] = mapped_column(ForeignKey("platforms.id"), nullable=True, index=True)
    article_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id"), nullable=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("article_groups.id"), nullable=True)
    stop_before_publish: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否等待手动确认发布
    client_request_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    worker_lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    worker_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    platform = relationship("Platform", back_populates="publish_tasks")
    article = relationship("Article")
    group = relationship("ArticleGroup", back_populates="publish_tasks")
    accounts = relationship("PublishTaskAccount", back_populates="task", cascade="all, delete-orphan")
    records = relationship("PublishRecord", back_populates="task")
    logs = relationship("TaskLog", back_populates="task")


# 任务-账号关联表，带排序（轮询顺序）
class PublishTaskAccount(Base):
    __tablename__ = "publish_task_accounts"
    __table_args__ = (UniqueConstraint("task_id", "account_id", name="uq_publish_task_accounts_task_account"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("publish_tasks.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)  # 执行顺序

    task = relationship("PublishTask", back_populates="accounts")
    account = relationship("Account", back_populates="publish_task_accounts")


# 发布记录：一次具体的发布操作（一篇文章 × 一个账号）
class PublishRecord(Base):
    __tablename__ = "publish_records"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending', 'running', 'waiting_manual_publish', 'waiting_user_input', 'succeeded', 'failed', 'cancelled')",
            name="ck_publish_records_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("publish_tasks.id"), index=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), index=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey("platforms.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    publish_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)  # 发布成功后的 URL
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    snapshot_content_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_of_record_id: Mapped[int | None] = mapped_column(ForeignKey("publish_records.id"), nullable=True)  # 重试来源
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 租约到期时间，用于崩溃恢复

    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    task = relationship("PublishTask", back_populates="records")
    article = relationship("Article", back_populates="publish_records")
    platform = relationship("Platform")
    account = relationship("Account", back_populates="publish_records")
    retry_of = relationship("PublishRecord", remote_side=[id])
    logs = relationship("TaskLog", back_populates="record")


# 任务执行日志：记录每个步骤的详情，失败时可附带截图资源
class TaskLog(Base):
    __tablename__ = "task_logs"
    __table_args__ = (CheckConstraint("level in ('info', 'warn', 'error')", name="ck_task_logs_level"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("publish_tasks.id"), index=True)
    record_id: Mapped[int | None] = mapped_column(ForeignKey("publish_records.id"), nullable=True)
    level: Mapped[str] = mapped_column(String(20), default="info", index=True)  # info / warn / error
    message: Mapped[str] = mapped_column(Text)
    screenshot_asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"), nullable=True)  # 失败截图
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    task = relationship("PublishTask", back_populates="logs")
    record = relationship("PublishRecord", back_populates="logs")
    screenshot_asset = relationship("Asset", back_populates="task_logs")
