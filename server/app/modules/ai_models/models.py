"""AI 模型注册表 ORM。

一行 = 一个可选模型（写作 generation 或 格式·配图 ai_format），只存元数据：
label / model(litellm 串) / scope / base_url(中转地址) / api_key_env(环境变量名) /
is_enabled / is_default / sort_order。**密钥本体绝不入库**。

`is_default_key` 是配合唯一约束实现「每 scope 至多一个默认」的小技巧：
is_default=True 时 = scope 字符串，否则 NULL（MySQL 唯一索引允许多个 NULL、
拒绝重复非 NULL），由 service 在每次写入时与 is_default 同步。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from server.app.core.time import utcnow
from server.app.db.base import Base

# 模型用途；与 PromptTemplate.scope 同词汇：generation=写作，ai_format=格式/标题/配图
SCOPES = ("generation", "ai_format")


class AiModel(Base):
    __tablename__ = "ai_models"
    __table_args__ = (
        Index("ix_ai_models_scope_enabled", "scope", "is_enabled"),
        UniqueConstraint("scope", "is_default_key", name="uq_ai_models_scope_default"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    # litellm 模型串；"" = 用该 scope 的默认模型（ai_model / ai_format_model）
    model: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    scope: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # OpenAI 兼容网关 / Anthropic 中转地址；None = litellm 默认官方端点
    base_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # 环境变量名（非密钥本体）；None = 回落该 scope 的全局 Key
    api_key_env: Mapped[str | None] = mapped_column(String(80), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 计算列：is_default 为真 = scope，否则 NULL，配合 UniqueConstraint 限「每 scope 至多一个默认」
    is_default_key: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
