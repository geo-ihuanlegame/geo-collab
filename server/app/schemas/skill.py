import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator


class SkillRead(BaseModel):
    id: int
    name: str
    description: str | None
    file_stats: dict
    is_enabled: bool
    is_deleted: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("file_stats", mode="before")
    @classmethod
    def parse_file_stats(cls, v: object) -> dict:
        if isinstance(v, str):
            return json.loads(v)
        return v  # type: ignore[return-value]


class SkillPatch(BaseModel):
    is_enabled: bool
