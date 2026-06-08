"""技能模块 service 层 CRUD（已下线休眠）：/api/skills 不再挂载，新方案流不用 Skill，仅保留休眠。"""

from sqlalchemy.orm import Session

from server.app.modules.skills.models import Skill


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
    content: str,
    description: str | None = None,
) -> Skill:
    skill = Skill(name=name, content=content, description=description)
    db.add(skill)
    db.flush()
    return skill


def update_skill(
    db: Session,
    skill: Skill,
    *,
    name: str,
    content: str,
    description: str | None,
) -> Skill:
    skill.name = name
    skill.content = content
    skill.description = description
    db.flush()
    return skill


def patch_skill(db: Session, skill: Skill, *, is_enabled: bool) -> Skill:
    skill.is_enabled = is_enabled
    db.flush()
    return skill


def delete_skill(db: Session, skill: Skill) -> None:
    skill.is_deleted = True
    db.flush()
