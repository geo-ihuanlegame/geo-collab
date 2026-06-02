"""AI 生文模块 ORM 模型。"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from server.app.core.time import utcnow
from server.app.db.base import Base


class GenerationSession(Base):
    __tablename__ = "generation_sessions"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending','running','done','failed')",
            name="ck_gen_sessions_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    skill_id: Mapped[int | None] = mapped_column(ForeignKey("skills.id"), nullable=True)
    prompt_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("prompt_templates.id"), nullable=True
    )
    extra_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    article_ids: Mapped[str] = mapped_column(Text, default="[]")  # JSON array of int
    # 手动模式：选中的 question_item id 列表（JSON）；与 auto_count 互斥。
    question_item_ids: Mapped[str] = mapped_column(Text, default="[]")
    # 自动/手动都关联到某个问题池
    pool_id: Mapped[int | None] = mapped_column(ForeignKey("question_pools.id"), nullable=True)
    # 自动模式：要生几篇
    auto_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class QuestionPool(Base):
    """问题池：对应一张飞书多维表，承载一组待生成的问题单元。"""

    __tablename__ = "question_pools"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    # 飞书多维表定位（同步来源）
    feishu_app_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    feishu_table_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(default=False, index=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class QuestionItem(Base):
    """问题单元：一条 = 一篇文章。可含 1~N 个待融合问题（融合由提示词负责）。

    消费队列语义：生成成功后 status 置 'consumed' 并记 article_id（出队）；
    失败保持 'pending' 可重试；再同步时按 (pool_id, record_id) 去重，已消费的不复活。
    """

    __tablename__ = "question_items"
    __table_args__ = (
        UniqueConstraint("pool_id", "record_id", name="uq_question_items_pool_record"),
        CheckConstraint(
            "status in ('pending','consumed')",
            name="ck_question_items_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("question_pools.id"), index=True)
    # 飞书记录 id（同一池内唯一，用于 upsert 去重）
    record_id: Mapped[str] = mapped_column(String(255))
    # 飞书记录全部字段原样镜像（保留以备后续需求变化）
    fields: Mapped[dict] = mapped_column(JSON, default=dict)
    # 专用字段：飞书"提问词"列 → question_text（生文用）
    question_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 专用字段：飞书"分类板块"列 → category（手动/自动分组键）
    category: Mapped[str | None] = mapped_column(String(200), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    article_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id"), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CategoryUsage(Base):
    """记录每个 (池, 分类板块) 上次被自动选题使用的时间。
    自动选题按 last_used_at ASC（NULL FIRST）+ 表内位置 排序，实现"最近没上的板块优先"。"""

    __tablename__ = "category_usages"

    pool_id: Mapped[int] = mapped_column(ForeignKey("question_pools.id"), primary_key=True)
    category: Mapped[str] = mapped_column(String(200), primary_key=True)
    last_used_at: Mapped[datetime] = mapped_column(DateTime)
