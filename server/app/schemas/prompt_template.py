from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PromptTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)


class PromptTemplateUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)


class PromptTemplateRead(BaseModel):
    id: int
    name: str
    content: str
    is_enabled: bool
    is_deleted: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PromptTemplatePatch(BaseModel):
    is_enabled: bool
