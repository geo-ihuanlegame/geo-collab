"""方案池服务：方案增删改查、问题类型聚合、校验 + 快照。

设计要点：
- 核心粒度是问题类型（QuestionItem.category，API 命名 question_type）。一条方案行 = 一个类型。
- 创建/更新都做完整校验，**校验全部通过后再落库**（更新时先验后改，避免半更新破坏已存方案）。
- 保存时把选中问题快照进 GenerationSchemeLineQuestion；运行只读快照，飞书后续改动不影响方案。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from server.app.modules.ai_generation import question_bank as qb
from server.app.modules.ai_generation.models import (
    GenerationScheme,
    GenerationSchemeLine,
    GenerationSchemeLineQuestion,
    GenerationSchemeRunTask,
    QuestionItem,
)
from server.app.modules.ai_generation.schemas import SchemeCreate, SchemeLineInput, SchemeUpdate
from server.app.modules.prompt_templates.service import get_visible_prompt_template
from server.app.shared.errors import ValidationError

# ── 读 ───────────────────────────────────────────────────────────────────────


def list_schemes(db: Session, *, user_id: int, is_admin: bool) -> list[GenerationScheme]:
    q = db.query(GenerationScheme).filter(GenerationScheme.is_deleted == False)  # noqa: E712
    if not is_admin:
        q = q.filter(GenerationScheme.user_id == user_id)
    return q.order_by(GenerationScheme.created_at.desc()).all()


def get_scheme(db: Session, scheme_id: int) -> GenerationScheme | None:
    return (
        db.query(GenerationScheme)
        .filter(
            GenerationScheme.id == scheme_id,
            GenerationScheme.is_deleted == False,  # noqa: E712
        )
        .first()
    )


def get_lines(db: Session, scheme_id: int) -> list[GenerationSchemeLine]:
    return (
        db.query(GenerationSchemeLine)
        .filter(GenerationSchemeLine.scheme_id == scheme_id)
        .order_by(GenerationSchemeLine.id.asc())
        .all()
    )


def get_line_questions(db: Session, scheme_line_id: int) -> list[GenerationSchemeLineQuestion]:
    return (
        db.query(GenerationSchemeLineQuestion)
        .filter(GenerationSchemeLineQuestion.scheme_line_id == scheme_line_id)
        .order_by(GenerationSchemeLineQuestion.id.asc())
        .all()
    )


def question_types(db: Session, pool_id: int) -> list[tuple[str | None, list[QuestionItem]]]:
    """按 category 聚合该池所有 source_active 的问题，保持首次出现顺序。"""
    items = (
        db.query(QuestionItem)
        .filter(
            QuestionItem.pool_id == pool_id,
            QuestionItem.source_active == True,  # noqa: E712
        )
        .order_by(QuestionItem.id.asc())
        .all()
    )
    buckets: dict[str | None, list[QuestionItem]] = {}
    order: list[str | None] = []
    for it in items:
        if it.category not in buckets:
            buckets[it.category] = []
            order.append(it.category)
        buckets[it.category].append(it)
    return [(k, buckets[k]) for k in order]


# ── 校验 ───────────────────────────────────────────────────────────────────────


def _norm_engine(value: str | None) -> str | None:
    """AI 引擎 model 字符串归一：空 / 纯空白 → None（运行时用系统默认模型）。"""
    if value is None:
        return None
    v = value.strip()
    return v or None


def _validate_template_ids(db: Session, *, template_ids: list[int], user_id: int) -> None:
    if not template_ids:
        raise ValidationError("每个问题类型至少要选一个提示词模板")
    for tid in dict.fromkeys(template_ids):
        tpl = get_visible_prompt_template(db, tid, user_id=user_id, scope="generation")
        if tpl is None:
            raise ValidationError(
                f"提示词模板不可用（不存在/已删除/不可见/非 generation 范围，id={tid}）"
            )
        if not tpl.is_enabled:
            raise ValidationError(f"提示词模板已停用（id={tid}）")


def _validate_line(
    db: Session, *, pool_id: int, line: SchemeLineInput, user_id: int
) -> list[QuestionItem]:
    if line.article_count is None or line.article_count <= 0:
        raise ValidationError("文章数必须大于 0")
    item_ids = list(dict.fromkeys(line.question_item_ids))
    if not item_ids:
        raise ValidationError("每个问题类型至少要选一个问题")
    items = db.query(QuestionItem).filter(QuestionItem.id.in_(item_ids)).all()
    if len(items) != len(item_ids):
        raise ValidationError("部分选中的问题不存在")
    for it in items:
        if it.pool_id != pool_id:
            raise ValidationError("选中的问题不属于该问题池")
        if not it.source_active:
            raise ValidationError(
                f"选中的问题已不在飞书（已失效，record_id={it.record_id}），请刷新后重选"
            )
        if it.category != line.question_type:
            raise ValidationError(
                f"问题类型不一致：问题 {it.record_id} 属于「{it.category}」，"
                f"方案行声明「{line.question_type}」"
            )
    _validate_template_ids(db, template_ids=line.allowed_prompt_template_ids, user_id=user_id)
    # 保持调用方传入的选择顺序
    by_id = {it.id: it for it in items}
    return [by_id[i] for i in item_ids]


def _build_line(
    db: Session, *, scheme_id: int, line: SchemeLineInput, items: list[QuestionItem]
) -> None:
    sline = GenerationSchemeLine(
        scheme_id=scheme_id,
        question_type=line.question_type,
        article_count=line.article_count,
        allowed_prompt_template_ids=list(dict.fromkeys(line.allowed_prompt_template_ids)),
    )
    db.add(sline)
    db.flush()
    for it in items:
        db.add(
            GenerationSchemeLineQuestion(
                scheme_line_id=sline.id,
                question_item_id=it.id,
                record_id=it.record_id,
                question_text=qb.question_text_of(it),
                question_type=it.category,
            )
        )


# ── 写 ───────────────────────────────────────────────────────────────────────


def create_scheme(
    db: Session, *, user_id: int, pool_id: int, payload: SchemeCreate
) -> GenerationScheme:
    if not payload.name or not payload.name.strip():
        raise ValidationError("方案名称不能为空")
    if not payload.lines:
        raise ValidationError("方案至少要有一个问题类型行")
    # 先全部校验通过，再落库
    validated = [
        (line, _validate_line(db, pool_id=pool_id, line=line, user_id=user_id))
        for line in payload.lines
    ]
    scheme = GenerationScheme(
        user_id=user_id,
        pool_id=pool_id,
        name=payload.name.strip(),
        is_enabled=payload.is_enabled,
        ai_engine=_norm_engine(payload.ai_engine),
    )
    db.add(scheme)
    db.flush()
    for line, items in validated:
        _build_line(db, scheme_id=scheme.id, line=line, items=items)
    db.flush()
    return scheme


def update_scheme(
    db: Session, *, scheme: GenerationScheme, user_id: int, payload: SchemeUpdate
) -> GenerationScheme:
    if not payload.name or not payload.name.strip():
        raise ValidationError("方案名称不能为空")
    if not payload.lines:
        raise ValidationError("方案至少要有一个问题类型行")
    # 先验后改：校验全部通过后才重建行/快照，避免半更新破坏已存方案
    validated = [
        (line, _validate_line(db, pool_id=scheme.pool_id, line=line, user_id=user_id))
        for line in payload.lines
    ]
    old_line_ids = [ln.id for ln in get_lines(db, scheme.id)]
    if old_line_ids:
        # 历次运行明细可能仍外键引用这些行（scheme_line_id, MySQL FK 默认 RESTRICT）。
        # 删行前先把引用置 NULL，否则 1451 → 未捕获 IntegrityError → 500。
        # 运行明细只读快照（question_type/question_text/... 已冗余），断开回指针不丢数据。
        db.query(GenerationSchemeRunTask).filter(
            GenerationSchemeRunTask.scheme_line_id.in_(old_line_ids)
        ).update({GenerationSchemeRunTask.scheme_line_id: None}, synchronize_session=False)
        db.query(GenerationSchemeLineQuestion).filter(
            GenerationSchemeLineQuestion.scheme_line_id.in_(old_line_ids)
        ).delete(synchronize_session=False)
        db.query(GenerationSchemeLine).filter(GenerationSchemeLine.scheme_id == scheme.id).delete(
            synchronize_session=False
        )
    scheme.name = payload.name.strip()
    scheme.is_enabled = payload.is_enabled
    scheme.ai_engine = _norm_engine(payload.ai_engine)
    db.flush()
    for line, items in validated:
        _build_line(db, scheme_id=scheme.id, line=line, items=items)
    db.flush()
    return scheme


def delete_scheme(db: Session, scheme: GenerationScheme) -> None:
    scheme.is_deleted = True
    db.flush()
