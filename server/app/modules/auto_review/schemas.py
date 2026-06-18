from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Decision = Literal["approved", "needs_rewrite", "rejected"]


class ScoreRequest(BaseModel):
    article_ids: list[int] = Field(..., min_length=1, max_length=20)
    dimensions: list[str] | None = None
    # None = use defaults: ["factuality", "readability", "style", "policy_safety"]


class ScoreBreakdown(BaseModel):
    article_id: int
    score_total: int
    score_breakdown: dict[str, int]
    suggested_decision: Decision
    reasoning: str


class ScoreResponse(BaseModel):
    results: list[ScoreBreakdown]


class AutoReviewSubmitRequest(BaseModel):
    decision: Decision
    score_total: int | None = None
    score_breakdown: dict[str, int] | None = None
    reasoning: str | None = None
    decided_by: str = "claude-code-loop"


class AutoReviewDecisionRead(BaseModel):
    id: int
    article_id: int
    decision: Decision
    score_total: int | None
    score_breakdown: dict[str, int] | None
    reasoning: str | None
    decided_by: str
    created_at: datetime

    class Config:
        from_attributes = True
