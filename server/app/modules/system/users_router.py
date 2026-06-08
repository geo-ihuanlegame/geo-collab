"""Current-user settings routes."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server.app.core.security import get_current_user, invalidate_user_cache
from server.app.db.session import get_db
from server.app.modules.audit.service import add_audit_entry
from server.app.modules.prompt_templates.service import get_visible_prompt_template
from server.app.modules.system.models import User

router = APIRouter()


class UserSettingsUpdate(BaseModel):
    ai_format_preset_id: int | None = None


class UserSettingsRead(BaseModel):
    ai_format_preset_id: int | None = None


@router.patch("/me/settings", response_model=UserSettingsRead)
def update_my_settings(
    payload: UserSettingsUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserSettingsRead:
    """更新当前用户的 AI 排版预设。非空时先校验该模板对本人可见且启用，否则 404。

    直接赋值，故传 null 会清空预设（与 ArticleUpdate 过滤 None 的语义不同）。
    改后 invalidate_user_cache 失效鉴权缓存。
    """
    if payload.ai_format_preset_id is not None:
        prompt = get_visible_prompt_template(
            db,
            payload.ai_format_preset_id,
            user_id=current_user.id,
            scope="ai_format",
        )
        if prompt is None or not prompt.is_enabled:
            raise HTTPException(status_code=404, detail="AI format prompt preset not found")

    user = db.get(User, current_user.id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.ai_format_preset_id = payload.ai_format_preset_id
    db.flush()
    invalidate_user_cache(user.id)
    add_audit_entry(
        db,
        user=current_user,
        action="user.settings.update",
        target_type="user",
        target_id=current_user.id,
        payload={"ai_format_preset_id": payload.ai_format_preset_id},
        request=request,
    )
    return UserSettingsRead(ai_format_preset_id=user.ai_format_preset_id)
