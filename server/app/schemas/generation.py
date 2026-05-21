import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator


class GenerationSessionCreate(BaseModel):
    skill_id: int
    prompt_template_id: int
    extra_instruction: str | None = None


class GenerationSessionRead(BaseModel):
    id: int
    status: str
    article_ids: list[int]
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None

    model_config = ConfigDict(from_attributes=True)

    @field_validator("article_ids", mode="before")
    @classmethod
    def parse_article_ids(cls, v: object) -> list[int]:
        if isinstance(v, str):
            return json.loads(v)
        return v  # type: ignore[return-value]
