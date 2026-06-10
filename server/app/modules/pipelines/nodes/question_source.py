"""question_source 源节点：按「问题类型 = 最小单元」组织。

每个类型一张卡，配置勾选问题（record_ids，省略/None=整类、自动跟进新同步问题）+ 允许模板 + 文章数。
输出扁平 question_text/question_count（保留给 ai_compose 等只认扁平文本的消费者）
+ generation_units（逐类型、仅含勾了≥1题的类型，供 ai_generate 逐单元生文）。
兼容旧扁平 config（question_types / question_record_ids / question_type）。"""

from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError

UNCATEGORIZED = "__uncategorized__"


def _coerce_count(v) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _build_units(cfg: dict, rows: list[tuple]) -> list[dict]:
    """rows: [(record_id, category, text)]（active、按 id 升序）。
    返回 [{question_type, texts, allowed_prompt_template_ids, article_count}]。"""
    units = cfg.get("units")
    if units is not None:
        out: list[dict] = []
        for u in units:
            if not isinstance(u, dict):
                continue
            qt = u.get("question_type")
            rids = u.get("record_ids")
            if rids is not None:
                rid_set = set(rids)
                texts = [
                    (t or "").strip()
                    for (rid, cat, t) in rows
                    if rid in rid_set and (t or "").strip()
                ]
            elif qt == UNCATEGORIZED:
                texts = [
                    (t or "").strip() for (rid, cat, t) in rows if cat is None and (t or "").strip()
                ]
            elif qt:
                texts = [
                    (t or "").strip() for (rid, cat, t) in rows if cat == qt and (t or "").strip()
                ]
            else:
                texts = []  # 缺 question_type 的整类单元视为无问题 → 被闸门丢弃
            out.append(
                {
                    "question_type": qt,
                    "texts": texts,
                    "allowed_prompt_template_ids": list(u.get("allowed_prompt_template_ids") or []),
                    "article_count": _coerce_count(u.get("article_count")),
                }
            )
        return out

    # 旧扁平 config → 按 category 分组成「无模板/无数量」单元（record_ids > types > 整池）
    question_types = cfg.get("question_types")
    if question_types is None:
        legacy = cfg.get("question_type")
        question_types = [] if (legacy is None or legacy == "") else [legacy]
    record_ids = cfg.get("question_record_ids") or []
    if record_ids:
        rid_set = set(record_ids)
        picked = [(rid, cat, t) for (rid, cat, t) in rows if rid in rid_set]
    elif question_types:
        named = {t for t in question_types if t != UNCATEGORIZED}
        incl_uncat = UNCATEGORIZED in question_types
        picked = [
            (rid, cat, t) for (rid, cat, t) in rows if cat in named or (incl_uncat and cat is None)
        ]
    else:
        picked = list(rows)

    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for _rid, cat, t in picked:
        key = cat if cat is not None else UNCATEGORIZED
        if key not in groups:
            groups[key] = []
            order.append(key)
        s = (t or "").strip()
        if s:
            groups[key].append(s)
    return [
        {
            "question_type": k,
            "texts": groups[k],
            "allowed_prompt_template_ids": [],
            "article_count": None,
        }
        for k in order
    ]


def run_question_source(ctx: NodeRunContext) -> NodeResult:
    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool

    cfg = ctx.config or {}
    pool_id = cfg.get("pool_id")
    if not pool_id:
        raise ValidationError("question_source 节点需配置 pool_id")

    db = ctx.session_factory()
    try:
        pool = db.get(QuestionPool, pool_id)
        if pool is None or getattr(pool, "is_deleted", False):
            raise ValidationError("问题池不存在")
        rows = (
            db.query(QuestionItem.record_id, QuestionItem.category, QuestionItem.question_text)
            .filter(QuestionItem.pool_id == pool_id, QuestionItem.source_active.is_(True))
            .order_by(QuestionItem.id.asc())
            .all()
        )
    finally:
        db.close()

    units = _build_units(cfg, rows)

    gen_units: list[dict] = []
    flat_texts: list[str] = []
    for u in units:
        if not u["texts"]:
            continue  # 闸门：无问题 → 弃用
        rendered = "\n".join(f"{i}. {t}" for i, t in enumerate(u["texts"], start=1))
        gen_units.append(
            {
                "question_type": u["question_type"],
                "question_text": rendered,
                "allowed_prompt_template_ids": u["allowed_prompt_template_ids"],
                "article_count": u["article_count"],
            }
        )
        flat_texts.extend(u["texts"])

    flat_rendered = "\n".join(f"{i}. {t}" for i, t in enumerate(flat_texts, start=1))
    return NodeResult(
        output={
            "question_text": flat_rendered,
            "question_count": len(flat_texts),
            "generation_units": gen_units,
        },
        article_ids=[],
    )


register("question_source", run_question_source)
