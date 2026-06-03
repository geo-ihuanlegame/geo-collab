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
    source_active: bool = True
    status: str
    article_id: int | None

    model_config = ConfigDict(from_attributes=True)


class SyncResult(BaseModel):
    total: int
    added: int
    updated: int
    reactivated: int
    deactivated: int


# ── 方案池（scheme）─────────────────────────────────────────────────────────


class QuestionBrief(BaseModel):
    id: int
    record_id: str
    question_text: str | None

    model_config = ConfigDict(from_attributes=True)


class QuestionTypeRead(BaseModel):
    """按问题类型（category）聚合，给方案录入页展示每个类型下有哪些 active 问题。"""

    question_type: str | None
    count: int
    questions: list[QuestionBrief]


class SchemeLineInput(BaseModel):
    question_type: str | None = None
    question_item_ids: list[int] = []
    article_count: int = 1
    allowed_prompt_template_ids: list[int] = []


class SchemeCreate(BaseModel):
    name: str
    pool_id: int
    is_enabled: bool = True
    lines: list[SchemeLineInput] = []


class SchemeUpdate(BaseModel):
    name: str
    is_enabled: bool = True
    lines: list[SchemeLineInput] = []


class SchemeLineQuestionRead(BaseModel):
    question_item_id: int | None
    record_id: str | None
    question_text: str | None
    question_type: str | None

    model_config = ConfigDict(from_attributes=True)


class SchemeLineRead(BaseModel):
    id: int
    question_type: str | None
    article_count: int
    allowed_prompt_template_ids: list[int]
    questions: list[SchemeLineQuestionRead]


class SchemeRead(BaseModel):
    id: int
    name: str
    pool_id: int
    is_enabled: bool
    created_at: datetime
    updated_at: datetime
    lines: list[SchemeLineRead]


# ── 方案运行（scheme run）───────────────────────────────────────────────────


class SchemeRunTaskRead(BaseModel):
    id: int
    scheme_line_id: int | None
    question_type: str | None
    question_text: str | None
    question_item_ids: list[int]
    allowed_prompt_template_ids: list[int]
    actual_prompt_template_id: int | None
    status: str
    article_id: int | None
    error_message: str | None

    model_config = ConfigDict(from_attributes=True)


class SchemeRunRead(BaseModel):
    id: int
    scheme_id: int
    status: str
    article_ids: list[int]
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None
    tasks: list[SchemeRunTaskRead]


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
