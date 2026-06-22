"""performance service：聚合模板 / 账号的表现指标。

读 articles.metrics（D3 加的 JSON 列）+ publish_records 表，按维度聚合。
POC 期：用 SQL aggregate 或纯 Python 算（数据量小）；v2 加缓存层。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from server.app.core.time import utcnow
from server.app.modules.articles.models import Article


def get_template_performance(
    db: Session,
    template_id: int,
    window_days: int = 7,
) -> dict[str, Any]:
    """聚合某个 prompt template 在窗口期内产出文章的指标。

    返回:
        {
          "template_id": int,
          "window_days": int,
          "article_count": int,
          "avg_views": float | None,
          "avg_likes": float | None,
          "approval_rate": float | None,  # 经自动审核 approved 的占比
        }
    """
    # POC: articles 没有直接 template_id 字段（生成后不存 source template id）——
    # 暂时返回空结构，D6 决定是否补 article.source_template_id 字段
    # 或者：用 audit_logs 查找"哪些 article 由这个 template compose 出来"
    # 简化：先返回 stub，让 MCP tool 链路通
    return {
        "template_id": template_id,
        "window_days": window_days,
        "article_count": 0,
        "avg_views": None,
        "avg_likes": None,
        "approval_rate": None,
        "note": "POC stub — 评估聚合实现待补 (compose_once 加 source_template_id 后填)",
    }


def get_account_performance(
    db: Session,
    account_id: int,
    window_days: int = 7,
) -> dict[str, Any]:
    """聚合某账号窗口期内已发布文章的指标。"""
    since = utcnow() - timedelta(days=window_days)
    # 通过 publish_records 找该账号的发布、再 join articles.metrics
    from server.app.modules.tasks.models import PublishRecord

    records = (
        db.query(PublishRecord)
        .filter(
            PublishRecord.account_id == account_id,
            PublishRecord.status == "succeeded",
            PublishRecord.finished_at >= since,
        )
        .all()
    )
    article_ids = [r.article_id for r in records if r.article_id]
    articles = db.query(Article).filter(Article.id.in_(article_ids)).all() if article_ids else []
    views = []
    likes = []
    for a in articles:
        if a.metrics:
            if (v := a.metrics.get("views")) is not None:
                views.append(v)
            if (lk := a.metrics.get("likes")) is not None:
                likes.append(lk)
    return {
        "account_id": account_id,
        "window_days": window_days,
        "publish_count": len(records),
        "with_metrics_count": len([a for a in articles if a.metrics]),
        "avg_views": (sum(views) / len(views)) if views else None,
        "avg_likes": (sum(likes) / len(likes)) if likes else None,
    }


def record_publish_metrics(
    db: Session,
    record_id: int,
    metrics: dict[str, Any],
) -> None:
    """写回某条 publish_record 对应 article 的 metrics（合并到 article.metrics JSON）。"""
    from server.app.modules.tasks.models import PublishRecord

    record = db.query(PublishRecord).filter(PublishRecord.id == record_id).first()
    if record is None:
        raise ValueError(f"publish_record not found: {record_id}")
    article = db.query(Article).filter(Article.id == record.article_id).first()
    if article is None:
        raise ValueError(f"article not found for record {record_id}: {record.article_id}")
    existing = dict(article.metrics or {})
    existing.update(metrics)
    existing.setdefault("recorded_at", utcnow().isoformat() + "Z")
    article.metrics = existing
