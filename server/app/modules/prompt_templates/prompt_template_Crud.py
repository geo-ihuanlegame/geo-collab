from sqlalchemy.orm import Session

from server.app.models.skill import PromptTemplate


def list_prompt_templates(db: Session) -> list[PromptTemplate]:
    return (
        db.query(PromptTemplate)
        .filter(PromptTemplate.is_deleted == False)  # noqa: E712
        .order_by(PromptTemplate.id)
        .all()
    )


def get_prompt_template(db: Session, template_id: int) -> PromptTemplate | None:
    return (
        db.query(PromptTemplate)
        .filter(PromptTemplate.id == template_id, PromptTemplate.is_deleted == False)  # noqa: E712
        .first()
    )


def create_prompt_template(db: Session, *, name: str, content: str) -> PromptTemplate:
    template = PromptTemplate(name=name, content=content)
    db.add(template)
    db.flush()
    return template


def update_prompt_template(
    db: Session, template: PromptTemplate, *, name: str, content: str
) -> PromptTemplate:
    template.name = name
    template.content = content
    db.flush()
    return template


def patch_prompt_template(
    db: Session, template: PromptTemplate, *, is_enabled: bool
) -> PromptTemplate:
    template.is_enabled = is_enabled
    db.flush()
    return template


def delete_prompt_template(db: Session, template: PromptTemplate) -> None:
    template.is_deleted = True
    db.flush()
