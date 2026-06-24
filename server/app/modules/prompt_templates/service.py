"""提示词模板 service 层：CRUD + 可见性过滤 + scope 校验。

可见性规则只作用于 *_visible_* 系列查询（list_visible_prompts / get_visible_prompt_template，
生文流走这条）：用户只看得到自己的私有模板或系统模板。不带 visible 的 list/get 只排除软删、不做可见性过滤。
软删（is_deleted）记录一律排除；所有写入只 flush 不 commit，事务边界交给上层（route 的 get_db）。
"""

from sqlalchemy import or_
from sqlalchemy.orm import Session

from server.app.modules.prompt_templates.models import PromptTemplate
from server.app.shared.errors import ValidationError

VALID_PROMPT_SCOPES = {"generation", "ai_format", "image_search", "image_companion"}


def _validate_scope(scope: str | None) -> None:
    # 非法 scope 抛命名异常 ValidationError（→400），不抛裸 ValueError（无全局兜底）
    if scope is not None and scope not in VALID_PROMPT_SCOPES:
        raise ValidationError(f"Invalid prompt scope: {scope}")


def _visible_query(db: Session, *, user_id: int, scope: str | None = None):
    """构造"当前用户可见"的基础查询：未软删 且（属于本人 或 系统模板）。"""
    _validate_scope(scope)
    query = db.query(PromptTemplate).filter(
        PromptTemplate.is_deleted == False,  # noqa: E712
        or_(PromptTemplate.user_id == user_id, PromptTemplate.is_system == True),  # noqa: E712
    )
    if scope is not None:
        query = query.filter(PromptTemplate.scope == scope)
    return query


def list_prompt_templates(db: Session, *, scope: str | None = None) -> list[PromptTemplate]:
    """全量列出（不做可见性过滤），仅排除软删（不过滤 is_enabled）。

    调用方：MCP catalog（service token，给 loop 列全量）+ 管理列表接口的 admin 分支
    （admin 看全量含其他用户私有模板）。系统模板排在前，与 list_visible_prompts 的排序对齐。
    """
    _validate_scope(scope)
    query = db.query(PromptTemplate).filter(PromptTemplate.is_deleted == False)  # noqa: E712
    if scope is not None:
        query = query.filter(PromptTemplate.scope == scope)
    return query.order_by(PromptTemplate.is_system.desc(), PromptTemplate.id).all()


def list_visible_prompts(
    db: Session, *, user_id: int, scope: str | None = None
) -> list[PromptTemplate]:
    """列出当前用户可见的模板（本人私有 + 系统），系统模板排在前。"""
    return (
        _visible_query(db, user_id=user_id, scope=scope)
        .order_by(PromptTemplate.is_system.desc(), PromptTemplate.id)
        .all()
    )


def get_prompt_template(db: Session, template_id: int) -> PromptTemplate | None:
    return (
        db.query(PromptTemplate)
        .filter(PromptTemplate.id == template_id, PromptTemplate.is_deleted == False)  # noqa: E712
        .first()
    )


def get_visible_prompt_template(
    db: Session,
    template_id: int,
    *,
    user_id: int,
    scope: str | None = None,
) -> PromptTemplate | None:
    return (
        _visible_query(db, user_id=user_id, scope=scope)
        .filter(PromptTemplate.id == template_id)
        .first()
    )


def get_active_template_content(
    db: Session, *, scope: str, user_id: int | None, default: str
) -> str:
    """取该 scope「当前生效」模板的内容（用于全局调优旋钮，如搜图词/陪衬提示词）。

    语义=「当前启用的那一条」（区别于 generation/ai_format 的按 id 随机抽）：在用户可见
    （本人私有 + 系统）、未软删、已启用的同 scope 模板里，本人模板优先于系统、再按最近更新取首条。
    无 user_id（理论上不该发生在 web_fallback 路径）或无命中 → 返回 default。
    用户靠启停切换做 A/B：同 scope 同时只启用一条即确定。
    """
    _validate_scope(scope)
    if user_id is None:
        return default
    template = (
        _visible_query(db, user_id=user_id, scope=scope)
        .filter(PromptTemplate.is_enabled == True)  # noqa: E712
        # is_system asc → 本人模板(False<True)排前；再按最近更新
        .order_by(PromptTemplate.is_system.asc(), PromptTemplate.updated_at.desc())
        .first()
    )
    if template is None or not (template.content or "").strip():
        return default
    return template.content


def create_prompt_template(
    db: Session,
    *,
    name: str,
    content: str,
    scope: str = "generation",
    user_id: int | None = None,
    is_system: bool = False,
) -> PromptTemplate:
    _validate_scope(scope)
    template = PromptTemplate(
        name=name,
        content=content,
        scope=scope,
        user_id=user_id,
        is_system=is_system,
    )
    db.add(template)
    db.flush()
    return template


def update_prompt_template(
    db: Session,
    template: PromptTemplate,
    *,
    name: str,
    content: str,
    scope: str | None = None,
    is_system: bool | None = None,
) -> PromptTemplate:
    template.name = name
    template.content = content
    if scope is not None:
        _validate_scope(scope)
        template.scope = scope
    if is_system is not None:
        template.is_system = is_system
    db.flush()
    return template


def patch_prompt_template(
    db: Session,
    template: PromptTemplate,
    *,
    is_enabled: bool | None = None,
    scope: str | None = None,
    is_system: bool | None = None,
) -> PromptTemplate:
    if is_enabled is not None:
        template.is_enabled = is_enabled
    if scope is not None:
        _validate_scope(scope)
        template.scope = scope
    if is_system is not None:
        template.is_system = is_system
    db.flush()
    return template


def delete_prompt_template(db: Session, template: PromptTemplate) -> None:
    # 软删：只置 is_deleted，不物理删行（历史引用/审计可追溯）
    template.is_deleted = True
    db.flush()
