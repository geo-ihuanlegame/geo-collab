import hmac
import os
import time
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request
from jose import jwt

from server.app.modules.system.models import User

JWT_ALGORITHM = "HS256"

# 内存用户缓存：避免每个请求都查一次 DB
# key=user_id, value=(User detached object, expire_monotonic)
_user_cache: dict[int, tuple[User, float]] = {}
_USER_CACHE_TTL = 60.0  # 60 秒 TTL，对 5 人内部平台足够


def _get_cached_user(user_id: int) -> User | None:
    entry = _user_cache.get(user_id)
    if entry and time.monotonic() < entry[1]:
        return entry[0]
    return None


def invalidate_user_cache(user_id: int) -> None:
    """用户状态变更时（禁用/改密码）主动清除缓存。"""
    _user_cache.pop(user_id, None)


def _reset_user_cache() -> None:
    """测试隔离用：清除全部缓存条目。"""
    _user_cache.clear()


def _get_jwt_secret() -> str:
    from server.app.core.config import get_settings

    return get_settings().jwt_secret


def _get_jwt_expire_hours() -> int:
    return int(os.environ.get("GEO_JWT_EXPIRE_HOURS", "8"))


def create_access_token(user_id: int, role: str) -> str:
    expire = datetime.now(UTC) + timedelta(hours=_get_jwt_expire_hours())
    payload = {"sub": str(user_id), "role": role, "exp": expire}
    return jwt.encode(payload, _get_jwt_secret(), algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, _get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    except Exception:
        return None


def get_current_user(request: Request) -> User:
    from sqlalchemy.orm import Session

    from server.app.db.session import SessionLocal

    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="未登录，请重新登录")
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")

    user_id = int(payload["sub"])
    user = _get_cached_user(user_id)

    if user is None:
        db: Session = SessionLocal()
        try:
            user = db.get(User, user_id)
            if not user:
                raise HTTPException(status_code=401, detail="用户不存在")
            # 从 Session 中脱钩后缓存，User 没有懒加载关联，脱钩后列值仍可访问
            db.expunge(user)
            _user_cache[user_id] = (user, time.monotonic() + _USER_CACHE_TTL)
        finally:
            db.close()

    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已被禁用")
    if user.must_change_password:
        raise HTTPException(status_code=403, detail="Password change required")
    return user


async def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return current_user


# NOTE: This function is currently unused (no route depends on it).
# Kept for potential future use. Remove if never adopted.
async def require_local_token(request: Request) -> None:
    token = os.environ.get("GEO_LOCAL_API_TOKEN")
    if not token:
        return

    received = request.headers.get("X-Geo-Token")
    if not received:
        raise HTTPException(status_code=401, detail="缺少认证令牌")

    if not hmac.compare_digest(token, received):
        raise HTTPException(status_code=401, detail="无效的认证令牌")
