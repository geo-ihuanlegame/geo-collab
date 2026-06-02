from sqlalchemy import or_
from sqlalchemy.orm import Session

from server.app.modules.prompt_templates.models import PromptTemplate
from server.app.shared.errors import ValidationError

VALID_PROMPT_SCOPES = {"generation", "ai_format"}


def _validate_scope(scope: str | None) -> None:
    if scope is not None and scope not in VALID_PROMPT_SCOPES:
        raise ValidationError(f"Invalid prompt scope: {scope}")


def _visible_query(db: Session, *, user_id: int, scope: str | None = None):
    _validate_scope(scope)
    query = db.query(PromptTemplate).filter(
        PromptTemplate.is_deleted == False,  # noqa: E712
        or_(PromptTemplate.user_id == user_id, PromptTemplate.is_system == True),  # noqa: E712
    )
    if scope is not None:
        query = query.filter(PromptTemplate.scope == scope)
    return query


def list_prompt_templates(db: Session, *, scope: str | None = None) -> list[PromptTemplate]:
    _validate_scope(scope)
    query = db.query(PromptTemplate).filter(PromptTemplate.is_deleted == False)  # noqa: E712
    if scope is not None:
        query = query.filter(PromptTemplate.scope == scope)
    return query.order_by(PromptTemplate.id).all()


def list_visible_prompts(
    db: Session, *, user_id: int, scope: str | None = None
) -> list[PromptTemplate]:
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
    template.is_deleted = True
    db.flush()
