"""认证与用户管理路由（/api/auth/* 与 /api/users/*）。

登录写 httpOnly cookie `access_token`。鉴权写法不统一：
- login 公开（仅限流）、logout 仅读 cookie，均不做鉴权；
- me / change-password / create-user 手动 verify_token + 自开 SessionLocal（早期写法，保留）；
- users 列表 / patch / reset-password 走 Depends(require_admin)。
审计：登录、登出、改密、建/改用户、重置密码等写操作落 add_audit_entry；me、users 列表等读操作不落。
"""

import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from server.app.core.config import get_settings
from server.app.core.limiter import limiter
from server.app.core.security import (
    create_access_token,
    invalidate_user_cache,
    require_admin,
    verify_token,
)
from server.app.core.time import utcnow
from server.app.db.session import get_db
from server.app.modules.audit.service import add_audit_entry
from server.app.modules.system.models import User

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8)


class CreateUserRequest(BaseModel):
    username: str
    password: str = Field(min_length=8)
    role: str = "operator"
    display_name: str | None = None


class UpdateUserRequest(BaseModel):
    is_active: bool | None = None
    role: str | None = None
    display_name: str | None = None
    feishu_open_id: str | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8)


def _user_dict(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "role": u.role,
        "is_active": u.is_active,
        "must_change_password": u.must_change_password,
        "display_name": u.display_name,
        "feishu_open_id": u.feishu_open_id,
        "ai_format_preset_id": u.ai_format_preset_id,
        "created_at": u.created_at,
        "last_login_at": u.last_login_at,
    }


@router.post("/login")
@limiter.limit("5/minute")
def login(
    request: Request, payload: LoginRequest, response: Response, db: Session = Depends(get_db)
) -> dict:
    """校验账号密码 → 写 access_token cookie → 记审计。限流 5 次/分钟。

    失败分支用 try/raise/except 包裹，只为在重新抛出前补一条 success=False 的审计。
    """
    user = db.query(User).filter(User.username == payload.username).first()
    if not user or not user.check_password(payload.password):
        try:
            # 先抛再于 except 内补审计、最后重新抛出：保证失败登录也留痕
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        except HTTPException:
            add_audit_entry(
                db,
                user=None,
                action="user.login",
                target_type="user",
                target_id=None,
                payload={"username": payload.username, "success": False},
                request=request,
            )
            raise
    if not user.is_active:
        try:
            raise HTTPException(status_code=403, detail="账号已被禁用")
        except HTTPException:
            add_audit_entry(
                db,
                user=None,
                action="user.login",
                target_type="user",
                target_id=None,
                payload={"username": payload.username, "success": False},
                request=request,
            )
            raise

    user.last_login_at = utcnow()
    token = create_access_token(user.id, user.role)
    max_age = int(os.environ.get("GEO_JWT_EXPIRE_HOURS", "8")) * 3600
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=max_age,
        secure=get_settings().secure_cookie,
    )
    add_audit_entry(
        db,
        user=user,
        action="user.login",
        target_type="user",
        target_id=user.id,
        payload={"username": payload.username, "success": True},
        request=request,
    )
    return {
        "username": user.username,
        "role": user.role,
        "must_change_password": user.must_change_password,
        "ai_format_preset_id": user.ai_format_preset_id,
    }


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    """清空 access_token cookie（max_age=0）并记审计。无需校验当前登录态。"""
    response.set_cookie(
        key="access_token",
        value="",
        httponly=True,
        samesite="lax",
        path="/",
        max_age=0,
        secure=get_settings().secure_cookie,
    )
    add_audit_entry(
        db,
        user=None,
        action="user.logout",
        target_type="user",
        target_id=None,
        payload=None,
        request=request,
    )
    return {"detail": "Logged out"}


@router.get("/me")
def me(request: Request) -> dict:
    """返回当前登录用户概要。手动 verify_token + 自开 session（不走 Depends 鉴权）。"""
    from server.app.db.session import SessionLocal

    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="未登录，请重新登录")
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")

    db: Session = SessionLocal()
    try:
        user = db.get(User, int(payload["sub"]))
        if not user:
            raise HTTPException(status_code=401, detail="用户不存在")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="账号已被禁用")
        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "must_change_password": user.must_change_password,
            "ai_format_preset_id": user.ai_format_preset_id,
        }
    finally:
        db.close()


@router.post("/change-password")
def change_password(payload: ChangePasswordRequest, request: Request) -> dict:
    """用户自助改密：校验原密码 → set_password → 清 must_change_password。

    改后调 invalidate_user_cache 让鉴权缓存失效，避免旧权限/状态被缓存命中。
    """
    from server.app.db.session import SessionLocal

    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="未登录，请重新登录")
    jwt_payload = verify_token(token)
    if not jwt_payload:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")

    db: Session = SessionLocal()
    try:
        user = db.get(User, int(jwt_payload["sub"]))
        if not user:
            raise HTTPException(status_code=401, detail="用户不存在")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="账号已被禁用")
        if not user.check_password(payload.old_password):
            raise HTTPException(status_code=400, detail="原密码错误")
        user.set_password(payload.new_password)
        user.must_change_password = False
        db.commit()
        invalidate_user_cache(user.id)
        add_audit_entry(
            db,
            user=user,
            action="user.password.change",
            target_type="user",
            target_id=user.id,
            payload={"username": user.username},
            request=request,
        )
        return {"detail": "Password changed"}
    finally:
        db.close()


@router.post("/users")
def create_user(payload: CreateUserRequest, request: Request) -> dict:
    """管理员创建用户（默认 must_change_password=True，首登需改密）。

    admin 双重校验：先看 JWT role 声明，再回库核对 caller.role（防 token 签发后用户被降权 / 禁用）。
    用户名冲突抛 409。
    """
    from server.app.db.session import SessionLocal

    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="未登录，请重新登录")
    jwt_payload = verify_token(token)
    if not jwt_payload:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    if jwt_payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")

    db: Session = SessionLocal()
    try:
        caller = db.get(User, int(jwt_payload["sub"]))
        if not caller or not caller.is_active or caller.role != "admin":
            raise HTTPException(status_code=403, detail="需要管理员权限")
        existing = db.query(User).filter(User.username == payload.username).first()
        if existing:
            raise HTTPException(status_code=409, detail="用户名已存在")
        user = User(
            username=payload.username,
            role=payload.role,
            is_active=True,
            must_change_password=True,
            display_name=payload.display_name or None,
        )
        user.set_password(payload.password)
        db.add(user)
        db.commit()
        db.refresh(user)
        add_audit_entry(
            db,
            user=caller,
            action="user.create",
            target_type="user",
            target_id=user.id,
            payload={"username": user.username, "role": user.role},
            request=request,
        )
        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "is_active": user.is_active,
            "must_change_password": user.must_change_password,
            "ai_format_preset_id": user.ai_format_preset_id,
        }
    finally:
        db.close()


@router.get("/users")
def list_users(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[dict]:
    users = db.query(User).order_by(User.created_at).all()
    return [_user_dict(u) for u in users]


@router.patch("/users/{user_id}")
def update_user(
    user_id: int,
    payload: UpdateUserRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> dict:
    """管理员改用户（启用/角色/显示名/飞书 open_id）。禁止改自己（防自锁/自降权）。

    只对真正变化的字段记 before/after 审计；feishu_open_id 变更不计入审计 diff。
    """
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="不能修改自己的账号")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    before: dict = {}
    after: dict = {}
    if payload.is_active is not None and payload.is_active != user.is_active:
        before["is_active"] = user.is_active
        after["is_active"] = payload.is_active
        user.is_active = payload.is_active
    if payload.role is not None and payload.role != user.role:
        before["role"] = user.role
        after["role"] = payload.role
        user.role = payload.role
    if payload.display_name is not None:
        new_display = payload.display_name or None
        if new_display != user.display_name:
            before["display_name"] = user.display_name
            after["display_name"] = new_display
            user.display_name = new_display
    if payload.feishu_open_id is not None:
        user.feishu_open_id = payload.feishu_open_id or None
    db.flush()
    invalidate_user_cache(user_id)
    if before or after:
        add_audit_entry(
            db,
            user=current_user,
            action="user.update",
            target_type="user",
            target_id=user_id,
            payload={"before": before, "after": after},
            request=request,
        )
    return _user_dict(user)


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    payload: ResetPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> dict:
    """管理员重置他人密码：强制下次登录改密，并 invalidate_user_cache 失效鉴权缓存。"""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    user.set_password(payload.new_password)
    user.must_change_password = True
    db.flush()
    invalidate_user_cache(user_id)
    add_audit_entry(
        db,
        user=current_user,
        action="user.password.reset",
        target_type="user",
        target_id=user_id,
        payload={"username": user.username},
        request=request,
    )
    return {"detail": "Password reset"}
