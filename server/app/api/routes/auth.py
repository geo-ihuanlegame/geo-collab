import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from server.app.core.config import get_settings
from server.app.core.time import utcnow
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from server.app.core.limiter import limiter
from server.app.core.security import create_access_token, get_current_user, invalidate_user_cache, require_admin, verify_token
from server.app.db.session import get_db
from server.app.models.user import User

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
        "created_at": u.created_at,
        "last_login_at": u.last_login_at,
    }


@router.post("/login")
@limiter.limit("5/minute")
def login(request: Request, payload: LoginRequest, response: Response, db: Session = Depends(get_db)) -> dict:
    user = db.query(User).filter(User.username == payload.username).first()
    if not user or not user.check_password(payload.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")

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
    return {
        "username": user.username,
        "role": user.role,
        "must_change_password": user.must_change_password,
    }


@router.post("/logout")
def logout(response: Response) -> dict:
    response.set_cookie(
        key="access_token",
        value="",
        httponly=True,
        samesite="lax",
        path="/",
        max_age=0,
        secure=get_settings().secure_cookie,
    )
    return {"detail": "Logged out"}


@router.get("/me")
def me(request: Request) -> dict:
    from server.app.db.session import SessionLocal

    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    db: Session = SessionLocal()
    try:
        user = db.get(User, int(payload["sub"]))
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account disabled")
        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "must_change_password": user.must_change_password,
        }
    finally:
        db.close()


@router.post("/change-password")
def change_password(payload: ChangePasswordRequest, request: Request) -> dict:
    from server.app.db.session import SessionLocal

    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    jwt_payload = verify_token(token)
    if not jwt_payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    db: Session = SessionLocal()
    try:
        user = db.get(User, int(jwt_payload["sub"]))
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account disabled")
        if not user.check_password(payload.old_password):
            raise HTTPException(status_code=400, detail="Old password is incorrect")
        user.set_password(payload.new_password)
        user.must_change_password = False
        db.commit()
        invalidate_user_cache(user.id)
        return {"detail": "Password changed"}
    finally:
        db.close()


@router.post("/users")
def create_user(payload: CreateUserRequest, request: Request) -> dict:
    from server.app.db.session import SessionLocal

    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    jwt_payload = verify_token(token)
    if not jwt_payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if jwt_payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin required")

    db: Session = SessionLocal()
    try:
        caller = db.get(User, int(jwt_payload["sub"]))
        if not caller or not caller.is_active or caller.role != "admin":
            raise HTTPException(status_code=403, detail="Admin required")
        existing = db.query(User).filter(User.username == payload.username).first()
        if existing:
            raise HTTPException(status_code=409, detail="Username already exists")
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
        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "is_active": user.is_active,
            "must_change_password": user.must_change_password,
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
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> dict:
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot modify your own account")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.role is not None:
        user.role = payload.role
    if payload.display_name is not None:
        user.display_name = payload.display_name or None
    if payload.feishu_open_id is not None:
        user.feishu_open_id = payload.feishu_open_id or None
    db.flush()
    invalidate_user_cache(user_id)
    return _user_dict(user)


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    payload: ResetPasswordRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.set_password(payload.new_password)
    user.must_change_password = True
    db.flush()
    invalidate_user_cache(user_id)
    return {"detail": "Password reset"}
