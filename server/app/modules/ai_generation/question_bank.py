"""问题库服务：池 CRUD、飞书镜像同步、默认取法。

核心语义（方案流，纯镜像）：
- 同步按 (pool_id, record_id) upsert；飞书存在则 source_active=True，飞书缺失则软标记
  source_active=False + source_deleted_at（不物理删除），再次出现则恢复 active。
- 对所有本地项一视同仁对齐飞书（含历史遗留 status='consumed' 的项），不再"消费不复活"。
- `status` / `article_id` / mark_*_consumed / auto_pick_groups / CategoryUsage 是旧
  /sessions 消费队列遗留，方案流不使用（保留为只读兼容 / 后续清理）。
- extract_question_text 是"记录→提示词"的**默认取法**，做成单一可替换函数。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from server.app.core.time import utcnow
from server.app.modules.ai_generation.models import CategoryUsage, QuestionItem, QuestionPool
from server.app.shared.errors import ClientError, ValidationError

# 飞书表里的字段名（同步时按这些名抽进专用列；如表头变了改这里即可）
FIELD_QUESTION = "提问词"
FIELD_CATEGORY = "分类板块"


# ── 池 CRUD ─────────────────────────────────────────────────────────────────


def list_pools(db: Session, *, user_id: int, is_admin: bool) -> list[QuestionPool]:
    q = db.query(QuestionPool).filter(QuestionPool.is_deleted == False)  # noqa: E712
    if not is_admin:
        q = q.filter(QuestionPool.user_id == user_id)
    return q.order_by(QuestionPool.created_at.desc()).all()


def get_pool(db: Session, pool_id: int) -> QuestionPool | None:
    return (
        db.query(QuestionPool)
        .filter(QuestionPool.id == pool_id, QuestionPool.is_deleted == False)  # noqa: E712
        .first()
    )


def create_pool(
    db: Session,
    *,
    user_id: int,
    name: str,
    feishu_app_token: str | None,
    feishu_table_id: str | None,
) -> QuestionPool:
    if not name or not name.strip():
        raise ValidationError("问题池名称不能为空")
    pool = QuestionPool(
        user_id=user_id,
        name=name.strip(),
        feishu_app_token=(feishu_app_token or None),
        feishu_table_id=(feishu_table_id or None),
    )
    db.add(pool)
    db.flush()
    return pool


# ── 同步（飞书 → 队列）──────────────────────────────────────────────────────


def sync_pool(db: Session, pool: QuestionPool) -> dict[str, int]:
    """从飞书多维表拉取记录，纯镜像 upsert。

    飞书存在 → 新增或更新内容并置 source_active=True、last_seen_at=now、清 source_deleted_at；
    飞书缺失 → 本地软标记 source_active=False、source_deleted_at=now（不物理删除）；
    缺失项再次出现 → 恢复 active。所有项一视同仁，不再跳过历史 consumed。
    成功时 pool.last_synced_at=now、last_sync_error=None。飞书读取失败抛 ClientError（不写错误，
    由定时同步 run_sync_once 捕获后单独记 last_sync_error）。
    """
    if not pool.feishu_app_token or not pool.feishu_table_id:
        raise ValidationError("该问题池未绑定飞书多维表（app_token/table_id 缺失）")

    # 延迟导入，避免无凭证环境在导入期报错
    from server.app.shared.feishu_bitable import FeishuError, list_bitable_records

    try:
        records = list_bitable_records(pool.feishu_app_token, pool.feishu_table_id)
    except FeishuError as exc:
        raise ClientError(f"飞书同步失败：{exc}") from exc

    existing = {
        it.record_id: it
        for it in db.query(QuestionItem).filter(QuestionItem.pool_id == pool.id).all()
    }
    now = utcnow()
    added = updated = reactivated = deactivated = 0
    seen: set[str] = set()

    for rec in records:
        record_id = rec.get("record_id")
        fields = rec.get("fields") or {}
        if not record_id:
            continue
        seen.add(record_id)
        # 从飞书记录里抽出生文真正要用的两列（其余列保存进 fields 备用，不参与流程）
        question_text = _stringify_field_value(fields.get(FIELD_QUESTION)).strip() or None
        category = _stringify_field_value(fields.get(FIELD_CATEGORY)).strip() or None
        cur = existing.get(record_id)
        if cur is None:
            db.add(
                QuestionItem(
                    pool_id=pool.id,
                    record_id=record_id,
                    fields=fields,
                    question_text=question_text,
                    category=category,
                    source_active=True,
                    last_seen_at=now,
                    synced_at=now,
                )
            )
            added += 1
        else:
            if not cur.source_active:
                reactivated += 1
            cur.fields = fields
            cur.question_text = question_text
            cur.category = category
            cur.source_active = True
            cur.source_deleted_at = None
            cur.last_seen_at = now
            cur.synced_at = now
            updated += 1

    # 飞书本轮缺失的本地项 → 软标记缺失（不物理删除）
    for record_id, cur in existing.items():
        if record_id in seen:
            continue
        if cur.source_active:
            cur.source_active = False
            cur.source_deleted_at = now
            deactivated += 1

    pool.last_synced_at = now
    pool.last_sync_error = None
    db.flush()
    return {
        "total": len(records),
        "added": added,
        "updated": updated,
        "reactivated": reactivated,
        "deactivated": deactivated,
    }


# ── 队列读取 / 出队 ──────────────────────────────────────────────────────────


def list_items(db: Session, pool_id: int, *, status: str | None = "pending") -> list[QuestionItem]:
    q = db.query(QuestionItem).filter(QuestionItem.pool_id == pool_id)
    if status is not None:
        q = q.filter(QuestionItem.status == status)
    return q.order_by(QuestionItem.id.asc()).all()


def get_items(db: Session, item_ids: list[int]) -> list[QuestionItem]:
    if not item_ids:
        return []
    return db.query(QuestionItem).filter(QuestionItem.id.in_(item_ids)).all()


def mark_consumed(db: Session, item_id: int, article_id: int) -> None:
    """生成成功后出队：置 consumed 并记 article_id。"""
    item = db.query(QuestionItem).filter(QuestionItem.id == item_id).first()
    if item is None:
        return
    item.status = "consumed"
    item.article_id = article_id
    db.flush()


# ── 默认取法（记录 → 提示词文本，可替换）─────────────────────────────────────


def _stringify_field_value(value: Any) -> str:
    """把飞书字段值（可能是 str / list[段] / dict / 数字）转成可读文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = []
        for seg in value:
            if isinstance(seg, dict):
                # 富文本段 {"type":"text","text":"..."} 或人员/链接等
                parts.append(str(seg.get("text") or seg.get("name") or seg.get("link") or ""))
            else:
                parts.append(_stringify_field_value(seg))
        return "".join(p for p in parts if p)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or value.get("link") or "")
    return str(value)


def extract_question_text(fields: dict) -> str:
    """旧默认取法：把记录所有字段拼成"字段名：值"多行文本。
    仅供同步前的旧 items（没有 question_text 列）回退使用；新数据请用 question_text_of。"""
    lines = []
    for name, value in (fields or {}).items():
        text = _stringify_field_value(value).strip()
        if text:
            lines.append(f"{name}：{text}")
    return "\n".join(lines)


# ── 取问题文本 / 多问题合并 ────────────────────────────────────────────────


def question_text_of(item: QuestionItem) -> str:
    """单条 item 的问题文本：优先 question_text 列，回退 fields 拍平。"""
    if item.question_text:
        return item.question_text.strip()
    return extract_question_text(item.fields or {}).strip()


def format_question_group(items: list[QuestionItem]) -> str:
    """把同一篇文章的多条问题渲染成编号列表，供拼进 user prompt。"""
    lines: list[str] = []
    for idx, it in enumerate(items, start=1):
        text = question_text_of(it)
        if text:
            lines.append(f"{idx}. {text}")
    return "\n".join(lines)


# ── 手动模式：按 category 分组 + 出队 ───────────────────────────────────────


def group_items_by_category(
    items: list[QuestionItem],
) -> list[tuple[str | None, list[QuestionItem]]]:
    """按 category 分组（None 单独一组），保持组的稳定顺序（首次出现顺序）。"""
    buckets: dict[str | None, list[QuestionItem]] = {}
    order: list[str | None] = []
    for it in items:
        key = it.category
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(it)
    return [(k, buckets[k]) for k in order]


def mark_items_consumed(db: Session, item_ids: list[int], article_id: int) -> None:
    """手动模式：把一批 items 同时标记 consumed 并关联到同一篇文章（与文章入库同一事务）。"""
    if not item_ids:
        return
    for item in db.query(QuestionItem).filter(QuestionItem.id.in_(item_ids)).all():
        item.status = "consumed"
        item.article_id = article_id
    db.flush()


# ── 自动模式：板块优先级 + 随机抽题 ─────────────────────────────────────────


def list_categories_for_auto(db: Session, pool_id: int) -> list[str]:
    """返回该池里"还有 pending 行的板块"，按
    (last_used_at ASC NULLS FIRST, 表内位置 ASC) 排序。
    表内位置 = MIN(item.id) per category（同步顺序≈表序）。"""
    from sqlalchemy import func, select

    pos_subq = (
        select(
            QuestionItem.category.label("category"),
            func.min(QuestionItem.id).label("first_id"),
        )
        .where(
            QuestionItem.pool_id == pool_id,
            QuestionItem.status == "pending",
            QuestionItem.category.is_not(None),
        )
        .group_by(QuestionItem.category)
        .subquery()
    )
    rows = db.execute(
        select(pos_subq.c.category, pos_subq.c.first_id, CategoryUsage.last_used_at).select_from(
            pos_subq.outerjoin(
                CategoryUsage,
                (CategoryUsage.pool_id == pool_id)
                & (CategoryUsage.category == pos_subq.c.category),
            )
        )
    ).all()

    def _key(r):
        _, first_id, last_used = r
        # NULLS FIRST: None 排最前
        return (0 if last_used is None else 1, last_used or datetime.min, first_id)

    return [cat for (cat, _fid, _lu) in sorted(rows, key=_key)]


def auto_pick_groups(
    db: Session,
    pool_id: int,
    n: int,
    *,
    rng=None,
) -> list[tuple[str, list[QuestionItem]]]:
    """自动选题：按板块优先级轮转 N 次；每次 K=randint(1,len(板块pending))，随机抽 K 条。
    自动模式不消费行，行可在后续批次再被抽到。"""
    import random as _random

    if n <= 0:
        return []
    rng = rng or _random.Random()
    cats = list_categories_for_auto(db, pool_id)
    if not cats:
        return []

    out: list[tuple[str, list[QuestionItem]]] = []
    for i in range(n):
        cat = cats[i % len(cats)]
        rows = (
            db.query(QuestionItem)
            .filter(
                QuestionItem.pool_id == pool_id,
                QuestionItem.category == cat,
                QuestionItem.status == "pending",
            )
            .order_by(QuestionItem.id.asc())
            .all()
        )
        if not rows:
            continue
        k = rng.randint(1, len(rows))
        subset = rng.sample(rows, k)
        out.append((cat, subset))
    return out


def mark_category_used(db: Session, pool_id: int, category: str) -> None:
    """自动模式：成功后 upsert (pool, category) 的 last_used_at = now。"""
    now = utcnow()
    usage = (
        db.query(CategoryUsage)
        .filter(CategoryUsage.pool_id == pool_id, CategoryUsage.category == category)
        .first()
    )
    if usage is None:
        db.add(CategoryUsage(pool_id=pool_id, category=category, last_used_at=now))
    else:
        usage.last_used_at = now
    db.flush()
