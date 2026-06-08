"""技能模块 ORM 模型（从 models/skill.py 中提取 Skill 类）。

已下线休眠：/api/skills 不再挂载，skills 表保留不 drop、不写迁移。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from server.app.core.time import utcnow
from server.app.db.base import Base


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # MySQL 不允许 TEXT 列有字面 DEFAULT（错误 1101），只用 python 端 default
    content: Mapped[str] = mapped_column(Text, default="")
    storage_path: Mapped[str] = mapped_column(String(500), default="", server_default="")
    file_stats: Mapped[str] = mapped_column(Text, default="{}")  # 保留兼容老 row
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
