"""提示词模板模块 Pydantic schemas（原 schemas/prompt_template.py）。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# scope 取值收敛在此处；service 层另有 VALID_PROMPT_SCOPES 做运行时校验，两处需保持一致
# image_search（百度搜图关键词）/ image_companion（陪衬游戏插图提示词）见 ai_format 配图链路
PromptScope = Literal["generation", "ai_format", "image_search", "image_companion"]


class PromptTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    scope: PromptScope = "generation"
    is_system: bool = False


class PromptTemplateUpdate(BaseModel):
    """全量更新（PUT）：name/content 必填覆盖；scope/is_system 为 None 时保持原值不动。"""

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
    """局部更新（PATCH）：只有非 None 字段会被写入，主要用于启停开关 is_enabled。"""

    is_enabled: bool | None = None
    scope: PromptScope | None = None
    is_system: bool | None = None
