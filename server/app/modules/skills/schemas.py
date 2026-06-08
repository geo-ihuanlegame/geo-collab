"""技能模块 Pydantic schemas。

已下线休眠：/api/skills 不再挂载，仅保留休眠不删。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SkillRead(BaseModel):
    id: int
    name: str
    description: str | None
    content: str
    is_enabled: bool
    is_deleted: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SkillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    description: str | None = None


class SkillUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    description: str | None = None


class SkillPatch(BaseModel):
    is_enabled: bool
