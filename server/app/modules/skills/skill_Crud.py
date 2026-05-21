import json

from sqlalchemy.orm import Session

from server.app.models.skill import Skill


def list_skills(db: Session) -> list[Skill]:
    return db.query(Skill).filter(Skill.is_deleted == False).order_by(Skill.id).all()  # noqa: E712


def get_skill(db: Session, skill_id: int) -> Skill | None:
    return (
        db.query(Skill)
        .filter(Skill.id == skill_id, Skill.is_deleted == False)  # noqa: E712
        .first()
    )


def create_skill(
    db: Session,
    *,
    name: str,
    description: str | None,
    storage_path: str,
    file_stats: dict,
) -> Skill:
    skill = Skill(
        name=name,
        description=description,
        storage_path=storage_path,
        file_stats=json.dumps(file_stats, ensure_ascii=False),
    )
    db.add(skill)
    db.flush()
    return skill


def patch_skill(db: Session, skill: Skill, *, is_enabled: bool) -> Skill:
    skill.is_enabled = is_enabled
    db.flush()
    return skill


def delete_skill(db: Session, skill: Skill) -> None:
    skill.is_deleted = True
    db.flush()
