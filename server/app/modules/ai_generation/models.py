"""AI 生文模块 ORM 模型。"""

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
    # 池级同步状态（镜像化）：最近一次同步报错文案 + 是否参与定时自动同步
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class QuestionItem(Base):
    """问题单元：飞书多维表一行的本地镜像。

    镜像语义（方案流）：同步按 (pool_id, record_id) upsert；飞书存在则 source_active=True，
    飞书缺失则软标记 source_active=False（不物理删除），再次出现则恢复。
    `status` / `article_id` 是旧消费队列遗留字段，方案流不再写入，仅保留只读兼容。
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
    # 专用字段：飞书"分类板块"列 → category（问题类型，方案行的分组键）
    category: Mapped[str | None] = mapped_column(String(200), index=True, nullable=True)
    # 镜像状态：飞书在=True；飞书缺失软标记=False + source_deleted_at；last_seen_at 记最近被同步见到的时刻
    source_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", index=True
    )
    source_deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # ↓ 旧消费队列遗留字段，方案流只读兼容，不再写入
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    article_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id"), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CategoryUsage(Base):
    """记录每个 (池, 分类板块) 上次被自动选题使用的时间。
    自动选题按 last_used_at ASC（NULL FIRST）+ 表内位置 排序，实现"最近没上的板块优先"。

    注：旧 /sessions 自动模式遗留表。方案流不使用，保留不删（标注后续清理）。"""

    __tablename__ = "category_usages"

    pool_id: Mapped[int] = mapped_column(ForeignKey("question_pools.id"), primary_key=True)
    category: Mapped[str] = mapped_column(String(200), primary_key=True)
    last_used_at: Mapped[datetime] = mapped_column(DateTime)


# ── 方案池 / 方案运行（scheme flow）────────────────────────────────────────────


class GenerationScheme(Base):
    """方案头：一个可长期复用的生文方案定义，绑定一个问题池。"""

    __tablename__ = "generation_schemes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("question_pools.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    # 方案级 AI 引擎（litellm model 字符串；None / 空 = 用系统默认 GEO_AI_MODEL）。
    # 为后续接入更多写作模型留接口，可选列表由 settings.ai_engines 暴露。
    ai_engine: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class GenerationSchemeLine(Base):
    """方案行：一行 = 一个问题类型（category）。承载该类型的文章数与允许模板列表。"""

    __tablename__ = "generation_scheme_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    scheme_id: Mapped[int] = mapped_column(ForeignKey("generation_schemes.id"), index=True)
    # 问题类型 = QuestionItem.category（可为 None，对应"无分类"组）
    question_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    article_count: Mapped[int] = mapped_column(Integer, default=1)
    # 该类型允许使用的提示词模板 id 列表（JSON 数组）；运行时每篇随机抽一个
    allowed_prompt_template_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class GenerationSchemeLineQuestion(Base):
    """方案行选中的问题：外键 + 快照。运行时只读快照，飞书后续改动不影响已存方案。"""

    __tablename__ = "generation_scheme_line_questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    scheme_line_id: Mapped[int] = mapped_column(
        ForeignKey("generation_scheme_lines.id"), index=True
    )
    # 外键用于联动追溯（问题被删后置 NULL 不影响快照）
    question_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("question_items.id"), nullable=True
    )
    # 快照字段（执行稳定性的核心）
    record_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    question_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    question_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class GenerationSchemeRun(Base):
    """方案运行头：一次实际生文任务，独立于方案定义。"""

    __tablename__ = "generation_scheme_runs"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending','running','done','partial_failed','failed')",
            name="ck_scheme_runs_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scheme_id: Mapped[int] = mapped_column(ForeignKey("generation_schemes.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    article_ids: Mapped[list] = mapped_column(JSON, default=list)
    # 运行时从方案快照的 AI 引擎（运行期不变；None / 空 = 系统默认模型）
    ai_engine: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GenerationSchemeRunTask(Base):
    """方案运行明细：每篇文章一条。记录实际采用的模板、问题快照、产出文章与错误。"""

    __tablename__ = "generation_scheme_run_tasks"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending','running','done','failed')",
            name="ck_scheme_run_tasks_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("generation_scheme_runs.id"), index=True)
    scheme_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("generation_scheme_lines.id"), nullable=True
    )
    question_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 渲染好的编号问题列表（该方案行选中问题快照合并）
    question_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    question_item_ids: Mapped[list] = mapped_column(JSON, default=list)
    allowed_prompt_template_ids: Mapped[list] = mapped_column(JSON, default=list)
    actual_prompt_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("prompt_templates.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    article_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id"), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
