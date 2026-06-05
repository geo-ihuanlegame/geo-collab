# server/app/modules/pipelines/models.py
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
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from server.app.core.time import utcnow
from server.app.db.base import Base


class Pipeline(Base):
    __tablename__ = "pipelines"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    has_draft: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    type: Mapped[str] = mapped_column(String(20), default="general", server_default="general")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    ignore_exception: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    schedule_kind: Mapped[str] = mapped_column(String(20), default="none", server_default="none")
    schedule_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schedule_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schedule_weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    window_start: Mapped[Time | None] = mapped_column(Time, nullable=True)
    window_end: Mapped[Time | None] = mapped_column(Time, nullable=True)
    last_scheduled_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class PipelineNode(Base):
    __tablename__ = "pipeline_nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    pipeline_id: Mapped[int] = mapped_column(
        ForeignKey("pipelines.id", ondelete="CASCADE"), index=True
    )
    node_type: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    node_index: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    flow_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PipelineVersion(Base):
    __tablename__ = "pipeline_versions"
    __table_args__ = (
        UniqueConstraint(
            "pipeline_id",
            "version_no",
            name="uq_pipeline_versions_pipeline_version",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    pipeline_id: Mapped[int] = mapped_column(
        ForeignKey("pipelines.id", ondelete="CASCADE"), index=True
    )
    version_no: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending','running','done','partial_failed','failed')",
            name="ck_pipeline_runs_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    pipeline_id: Mapped[int] = mapped_column(
        ForeignKey("pipelines.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    node_results: Mapped[dict] = mapped_column(JSON, default=dict)
    article_ids: Mapped[list] = mapped_column(JSON, default=list)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
