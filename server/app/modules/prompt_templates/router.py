"""提示词模板模块路由。"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from server.app.core.security import get_current_user
from server.app.db.session import get_db
from server.app.modules.system.models import User
from server.app.modules.prompt_templates.service import (
    create_prompt_template,
    delete_prompt_template,
    get_prompt_template,
    list_prompt_templates,
    patch_prompt_template,
    update_prompt_template,
)
from server.app.modules.prompt_templates.schemas import (
    PromptTemplateCreate,
    PromptTemplatePatch,
    PromptTemplateRead,
    PromptTemplateUpdate,
)

router = APIRouter()


@router.get("", response_model=list[PromptTemplateRead])
def read_prompt_templates(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Any]:
    return list_prompt_templates(db)


@router.post("", response_model=PromptTemplateRead, status_code=201)
def create_prompt_template_route(
    payload: PromptTemplateCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    return create_prompt_template(db, name=payload.name, content=payload.content)


@router.put("/{template_id}", response_model=PromptTemplateRead)
def update_prompt_template_route(
    template_id: int,
    payload: PromptTemplateUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    template = get_prompt_template(db, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="提示词模板不存在")
    return update_prompt_template(db, template, name=payload.name, content=payload.content)


@router.patch("/{template_id}", response_model=PromptTemplateRead)
def patch_prompt_template_route(
    template_id: int,
    payload: PromptTemplatePatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    template = get_prompt_template(db, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="提示词模板不存在")
    return patch_prompt_template(db, template, is_enabled=payload.is_enabled)


@router.delete("/{template_id}", status_code=204)
def delete_prompt_template_route(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    template = get_prompt_template(db, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="提示词模板不存在")
    delete_prompt_template(db, template)
