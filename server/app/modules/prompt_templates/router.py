"""提示词模板路由。"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from server.app.core.security import get_current_user
from server.app.db.session import get_db
from server.app.modules.audit.service import add_audit_entry
from server.app.modules.prompt_templates.schemas import (
    PromptScope,
    PromptTemplateCreate,
    PromptTemplatePatch,
    PromptTemplateRead,
    PromptTemplateUpdate,
)
from server.app.modules.prompt_templates.service import (
    create_prompt_template,
    delete_prompt_template,
    get_prompt_template,
    list_prompt_templates,
    list_visible_prompts,
    patch_prompt_template,
    update_prompt_template,
)
from server.app.modules.system.models import User

router = APIRouter()


def _ensure_can_edit(template: Any, current_user: User) -> None:
    """编辑权限：admin 通吃；普通用户可编辑系统/共享模板与自己的模板，
    但不能编辑其他普通用户的私有模板。系统模板（如「基础」AI格式提示词）全员共享可改。"""
    if current_user.role == "admin":
        return
    if template.is_system or template.user_id == current_user.id:
        return
    raise HTTPException(status_code=403, detail="No permission to modify this prompt template")


def _ensure_can_delete(template: Any, current_user: User) -> None:
    """删除权限：admin 通吃；普通用户只能删自己的非系统模板。
    系统/共享模板的删除收归 admin（防误删全局资源）。"""
    if current_user.role == "admin":
        return
    if template.is_system or template.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only admin can delete system prompt templates")


def _ensure_system_change_allowed(
    requested_is_system: bool | None, current_is_system: bool, current_user: User
) -> None:
    """is_system 标记的变更收归 admin：普通用户尝试改变该标记直接 403；
    标记不变（如编辑系统模板时透传原值）则放行。requested 为 None 表示不动该字段。
    创建场景把 current 传 False 即可复用：普通用户置 True → 403。"""
    if current_user.role == "admin":
        return
    if requested_is_system is None:
        return
    if requested_is_system != current_is_system:
        raise HTTPException(
            status_code=403, detail="Only admin can create or manage system prompt templates"
        )


@router.get("", response_model=list[PromptTemplateRead])
def read_prompt_templates(
    scope: PromptScope | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Any]:
    # admin 看全量（含其他用户私有模板），普通用户只看本人 + 系统模板。
    # 编辑/删除权限早已是「admin 通吃」（_ensure_can_edit/_ensure_can_delete 走不过滤可见性的
    # get_prompt_template），此前唯一缺口是列表看不到别人的私有模板、UI 上点不进去。
    if current_user.role == "admin":
        return list_prompt_templates(db, scope=scope)
    return list_visible_prompts(db, user_id=current_user.id, scope=scope)


@router.post("", response_model=PromptTemplateRead, status_code=201)
def create_prompt_template_route(
    payload: PromptTemplateCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    _ensure_system_change_allowed(payload.is_system, False, current_user)
    template = create_prompt_template(
        db,
        name=payload.name,
        content=payload.content,
        scope=payload.scope,
        user_id=current_user.id,
        is_system=payload.is_system,
    )
    add_audit_entry(
        db,
        user=current_user,
        action="prompt_template.create",
        target_type="prompt_template",
        target_id=template.id,
        payload={"name": template.name},
        request=request,
    )
    return template


@router.put("/{template_id}", response_model=PromptTemplateRead)
def update_prompt_template_route(
    template_id: int,
    payload: PromptTemplateUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    template = get_prompt_template(db, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Prompt template not found")
    _ensure_can_edit(template, current_user)
    _ensure_system_change_allowed(payload.is_system, template.is_system, current_user)
    updated = update_prompt_template(
        db,
        template,
        name=payload.name,
        content=payload.content,
        scope=payload.scope,
        is_system=payload.is_system,
    )
    add_audit_entry(
        db,
        user=current_user,
        action="prompt_template.update",
        target_type="prompt_template",
        target_id=template_id,
        payload={"name": payload.name},
        request=request,
    )
    return updated


@router.patch("/{template_id}", response_model=PromptTemplateRead)
def patch_prompt_template_route(
    template_id: int,
    payload: PromptTemplatePatch,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    template = get_prompt_template(db, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Prompt template not found")
    _ensure_can_edit(template, current_user)
    _ensure_system_change_allowed(payload.is_system, template.is_system, current_user)
    result = patch_prompt_template(
        db,
        template,
        is_enabled=payload.is_enabled,
        scope=payload.scope,
        is_system=payload.is_system,
    )
    add_audit_entry(
        db,
        user=current_user,
        action="prompt_template.enable_toggle",
        target_type="prompt_template",
        target_id=template_id,
        payload={"is_enabled": payload.is_enabled},
        request=request,
    )
    return result


@router.delete("/{template_id}", status_code=204)
def delete_prompt_template_route(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    template = get_prompt_template(db, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Prompt template not found")
    _ensure_can_delete(template, current_user)
    template_name = template.name
    delete_prompt_template(db, template)
    add_audit_entry(
        db,
        user=current_user,
        action="prompt_template.delete",
        target_type="prompt_template",
        target_id=template_id,
        payload={"name": template_name},
        request=request,
    )
