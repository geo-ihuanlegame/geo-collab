"""技能模块路由 —— 单文本模型，与 PromptTemplate 同构。"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from server.app.core.security import get_current_user
from server.app.db.session import get_db
from server.app.modules.audit.service import add_audit_entry
from server.app.modules.system.models import User
from server.app.modules.skills.schemas import (
    SkillCreate,
    SkillPatch,
    SkillRead,
    SkillUpdate,
)
from server.app.modules.skills.service import (
    create_skill,
    delete_skill,
    get_skill,
    list_skills,
    patch_skill,
    update_skill,
)

router = APIRouter()


@router.get("", response_model=list[SkillRead])
def read_skills(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Any]:
    return list_skills(db)


@router.post("", response_model=SkillRead, status_code=201)
def create_skill_endpoint(
    payload: SkillCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    skill = create_skill(
        db,
        name=payload.name.strip(),
        content=payload.content,
        description=(payload.description or None),
    )
    add_audit_entry(
        db,
        user=current_user,
        action="skill.create",
        target_type="skill",
        target_id=skill.id,
        payload={"name": skill.name},
        request=request,
    )
    return SkillRead.model_validate(skill)


@router.put("/{skill_id}", response_model=SkillRead)
def update_skill_endpoint(
    skill_id: int,
    payload: SkillUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    skill = get_skill(db, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    updated = update_skill(
        db,
        skill,
        name=payload.name.strip(),
        content=payload.content,
        description=(payload.description or None),
    )
    add_audit_entry(
        db,
        user=current_user,
        action="skill.update",
        target_type="skill",
        target_id=skill_id,
        payload={"name": updated.name},
        request=request,
    )
    return SkillRead.model_validate(updated)


@router.patch("/{skill_id}", response_model=SkillRead)
def patch_skill_endpoint(
    skill_id: int,
    payload: SkillPatch,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    skill = get_skill(db, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    result = patch_skill(db, skill, is_enabled=payload.is_enabled)
    add_audit_entry(
        db,
        user=current_user,
        action="skill.enable_toggle",
        target_type="skill",
        target_id=skill_id,
        payload={"is_enabled": payload.is_enabled},
        request=request,
    )
    return result


@router.delete("/{skill_id}", status_code=204)
def remove_skill(
    skill_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    skill = get_skill(db, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    skill_name = skill.name
    delete_skill(db, skill)
    add_audit_entry(
        db,
        user=current_user,
        action="skill.delete",
        target_type="skill",
        target_id=skill_id,
        payload={"name": skill_name},
        request=request,
    )
