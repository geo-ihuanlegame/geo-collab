"""提示词模板模块 Pydantic schemas（原 schemas/prompt_template.py）。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PromptScope = Literal["generation", "ai_format"]


class PromptTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    scope: PromptScope = "generation"
    is_system: bool = False


class PromptTemplateUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    scope: PromptScope | None = None
    is_system: bool | None = None


class PromptTemplateRead(BaseModel):
    id: int
    name: str
    content: str
    scope: str
    user_id: int | None
    is_system: bool
    is_enabled: bool
    is_deleted: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PromptTemplatePatch(BaseModel):
    is_enabled: bool | None = None
    scope: PromptScope | None = None
    is_system: bool | None = None
