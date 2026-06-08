"""提示词模板模块 ORM 模型（从 models/skill.py 中提取 PromptTemplate 类）。"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from server.app.core.time import utcnow
from server.app.db.base import Base


class PromptTemplate(Base):
    """提示词模板。两种归属：系统模板（is_system）所有人可见、用户私有模板按 user_id 隔离。

    scope 区分用途：generation（写作）/ ai_format（排版/标题/配图），对应 CLAUDE.md 的两套模型配置。
    删除走软删（is_deleted），可见性/查询一律带 is_deleted==False 过滤，不物理删行。
    """

    __tablename__ = "prompt_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(
        String(50), default="generation", server_default="generation", index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_prompt_templates_user_id"), nullable=True, index=True
    )
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
