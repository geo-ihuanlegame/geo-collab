"""AI 生文模块 Pydantic schemas（原 schemas/generation.py）。"""

import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator


class GenerationSessionCreate(BaseModel):
    skill_id: int
    prompt_template_id: int
    extra_instruction: str | None = None
    # 问题池（手动/自动都需要；手动模式可由 items 反推但显式更稳）
    pool_id: int | None = None
    # 手动模式：选中的问题单元 id；按 category 分组 → 每组一篇
    question_item_ids: list[int] = []
    # 自动模式：要生几篇（与 question_item_ids 互斥；按板块轮转 + 随机抽题）
    auto_count: int | None = None


# ── 问题库 ───────────────────────────────────────────────────────────────────


class QuestionPoolCreate(BaseModel):
    name: str
    feishu_app_token: str | None = None
    feishu_table_id: str | None = None


class QuestionPoolRead(BaseModel):
    id: int
    name: str
    feishu_app_token: str | None
    feishu_table_id: str | None
    last_synced_at: datetime | None
    created_at: datetime
    pending_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class QuestionItemRead(BaseModel):
    id: int
    record_id: str
    fields: dict
    question_text: str | None
    category: str | None
    status: str
    article_id: int | None

    model_config = ConfigDict(from_attributes=True)


class SyncResult(BaseModel):
    total: int
    added: int
    updated: int
    skipped_consumed: int


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
