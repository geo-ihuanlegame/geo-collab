"""auto_review service：LLM 批量评分 + decision 持久化。

评分用 ai_format_model（deepseek-v4-flash 经济档），由 ai_models.service 解析。
失败容错：单条评分失败 → score_total=-1 + reasoning="[评分失败] ..." 仍入结果列表。
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

import litellm
from sqlalchemy import func
from sqlalchemy.orm import Session

from server.app.core.time import utcnow
from server.app.modules.ai_models.service import resolve_ai_format_model
from server.app.modules.articles.models import Article
from server.app.modules.auto_review.models import AutoReviewDecision
from server.app.modules.auto_review.schemas import (
    AutoReviewSubmitRequest,
    ScoreBreakdown,
    ScoreRequest,
)

_logger = logging.getLogger(__name__)

DEFAULT_DIMENSIONS = ["factuality", "readability", "style", "policy_safety"]


_SCORE_PROMPT_TEMPLATE = """你是「餐厅养成记」官方矩阵的内容评估官。

请评估下面这篇文章在以下维度的表现，每项 0-100 整数：
{dimensions_block}

文章正文（已截断到 4000 字）：
---
{plain_text}
---

只输出严格 JSON，不要 markdown / 代码块，键名严格匹配维度 key：
{{
  "score_breakdown": {{ "<key>": <int>, ... }},
  "score_total": <int 0-100>,
  "suggested_decision": "approved" | "needs_rewrite" | "rejected",
  "reasoning": "<1-2 句话>"
}}

判定规则建议：score_total >= 70 → approved；40-69 → needs_rewrite；< 40 → rejected。
"""

_DIM_LABELS = {
    "factuality": ("事实性", "事实陈述是否准确、有无明显错误或夸大"),
    "readability": ("可读性", "句子流畅、结构清晰、逻辑通顺"),
    "style": ("风格匹配", "符合餐厅养成记目标受众（休闲玩家、女性偏多、治愈调性）"),
    "policy_safety": ("政策安全", "无敏感话题、无违规、无诱导"),
}


def _format_dimensions(dimensions: list[str]) -> str:
    lines = []
    for i, key in enumerate(dimensions, 1):
        label, hint = _DIM_LABELS.get(key, (key, ""))
        lines.append(f"  {i}. {key}（{label}）：{hint}")
    return "\n".join(lines)


def score_articles(db: Session, req: ScoreRequest) -> list[ScoreBreakdown]:
    """批量评分。每条独立调 LLM，单条失败不影响其它。返回结果与 input 顺序一致。"""
    dimensions = req.dimensions or DEFAULT_DIMENSIONS
    model, api_key, base_url, timeout = resolve_ai_format_model(db, selected=None)

    results: list[ScoreBreakdown] = []
    articles = db.query(Article).filter(Article.id.in_(req.article_ids)).all()
    by_id = {a.id: a for a in articles}

    for aid in req.article_ids:
        a = by_id.get(aid)
        if a is None:
            results.append(
                ScoreBreakdown(
                    article_id=aid,
                    score_total=-1,
                    score_breakdown={k: 0 for k in dimensions},
                    suggested_decision="rejected",
                    reasoning="[评分失败] article not found",
                )
            )
            continue

        plain = (a.plain_text or "")[:4000]
        prompt = _SCORE_PROMPT_TEMPLATE.format(
            dimensions_block=_format_dimensions(dimensions),
            plain_text=plain,
        )

        try:
            resp = litellm.completion(
                model=model,
                api_key=api_key or None,
                api_base=base_url or None,
                timeout=timeout,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content or "{}"
            parsed = json.loads(content)
            results.append(
                ScoreBreakdown(
                    article_id=aid,
                    score_total=int(parsed.get("score_total", 0)),
                    score_breakdown={
                        k: int(parsed.get("score_breakdown", {}).get(k, 0)) for k in dimensions
                    },
                    suggested_decision=parsed.get("suggested_decision", "needs_rewrite"),
                    reasoning=parsed.get("reasoning", ""),
                )
            )
        except Exception as exc:  # noqa: BLE001 single-item failure doesn't affect others
            _logger.warning("score article %s failed: %s", aid, exc)
            results.append(
                ScoreBreakdown(
                    article_id=aid,
                    score_total=-1,
                    score_breakdown={k: 0 for k in dimensions},
                    suggested_decision="rejected",
                    reasoning=f"[评分失败] {exc}",
                )
            )

    return results


def submit_decision(
    db: Session,
    article_id: int,
    req: AutoReviewSubmitRequest,
) -> AutoReviewDecision:
    """写一条 AutoReviewDecision。注意：不动 article.review_status，最终人审兜底。"""
    article = db.query(Article).filter(Article.id == article_id).first()
    if article is None:
        raise ValueError(f"article not found: {article_id}")
    decision = AutoReviewDecision(
        article_id=article_id,
        decision=req.decision,
        score_total=req.score_total,
        score_breakdown=req.score_breakdown,
        reasoning=req.reasoning,
        decided_by=req.decided_by,
    )
    db.add(decision)
    db.flush()
    return decision


def list_recent_decisions(
    db: Session,
    *,
    decided_by: str,
    decision: str,
    since_hours: int,
    model_label: str | None = None,
    limit: int = 50,
) -> tuple[int, list[dict]]:
    """返回 (total_count, items[:limit])。

    items: [{article_id, title, decided_at, score_total}], newest first.
    decided_at 是 raw `datetime`（无时区 UTC），由 main.py 全局序列化器在响应层
    统一补 "Z" 后缀；service 自己不格式化避免双 Z。
    total_count 是滚动时间窗内全部命中行数（不被 limit 影响）。

    用于 `/goal` orchestrator 的净产出验证 —— 主对话每轮调一次拿 ground truth
    决定是否继续循环。

    Args:
        decided_by: AutoReviewDecision.decided_by 精确匹配。
        decision: AutoReviewDecision.decision 精确匹配。
        since_hours: 滚动时间窗（小时），从当前 UTC 时间往回数。
        model_label: 可选，进一步要求 Article.metrics.writer_model 等于此值。
            None 表示不过滤这个维度。
        limit: items 数组的截断上限；count 不受影响。
    """
    since = utcnow() - timedelta(hours=since_hours)

    q = (
        db.query(AutoReviewDecision, Article)
        .join(Article, Article.id == AutoReviewDecision.article_id)
        .filter(
            AutoReviewDecision.decided_by == decided_by,
            AutoReviewDecision.decision == decision,
            AutoReviewDecision.created_at >= since,
        )
    )
    if model_label:
        q = q.filter(
            func.json_unquote(func.json_extract(Article.metrics, "$.writer_model")) == model_label
        )

    total = q.count()
    rows = q.order_by(AutoReviewDecision.created_at.desc()).limit(limit).all()
    items = [
        {
            "article_id": a.id,
            "title": a.title,
            "decided_at": d.created_at,
            "score_total": d.score_total,
        }
        for d, a in rows
    ]
    return total, items
