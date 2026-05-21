"""
Geo Collab API 应用工厂与全局配置。

入口点：
  - 开发: uvicorn server.app.main:app --reload
  - 生产: docker-compose up（Dockerfile CMD 先跑 alembic upgrade head）

阅读顺序建议：
  1. create_app() → 了解路由注册、全局异常处理、启动行为
  2. models/publish.py → PublishTask / PublishRecord 状态机
  3. services/tasks.py → 任务执行引擎
  4. services/drivers/toutiao.py → 头条浏览器自动化
"""
import os
import sys
from datetime import datetime
from pathlib import Path

# ── datetime 序列化补丁 ──
# 在 Pydantic 模型定义之前安装，确保所有 naive datetime 输出带 "Z" 后缀
# 这样前端 new Date("2026-05-12T14:00:00Z") 能正确识别为 UTC
# 涉及三个层级：Pydantic 模型、FastAPI 内置编码器、FastAPI 构造函数

from pydantic import BaseModel

_orig_init_subclass = BaseModel.__init_subclass__


def _init_subclass_patch(cls, **kwargs):
    _orig_init_subclass(**kwargs)
    if cls.__name__ == "BaseModel":
        return
    encoders = dict(cls.model_config.get("json_encoders", {}))
    encoders.setdefault(
        datetime,
        lambda dt: dt.isoformat() + ("Z" if dt.tzinfo is None else ""),
    )
    cls.model_config["json_encoders"] = encoders


BaseModel.__init_subclass__ = classmethod(_init_subclass_patch)

import fastapi.encoders

fastapi.encoders.ENCODERS_BY_TYPE[datetime] = lambda dt: dt.isoformat() + ("Z" if dt.tzinfo is None else "")

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from server.app.api.routes.accounts import router as accounts_router
from server.app.api.routes.article_groups import router as article_groups_router
from server.app.api.routes.articles import router as articles_router
from server.app.api.routes.assets import router as assets_router
from server.app.api.routes.auth import router as auth_router
from server.app.api.routes.chunked_assets import router as chunked_assets_router
from server.app.api.routes.publish_records import router as publish_records_router
from server.app.api.routes.system import router as system_router
from server.app.api.routes.tasks import router as tasks_router
from server.app.core.config import get_settings
from server.app.core.paths import ensure_data_dirs
from server.app.core.security import get_current_user
from server.app.models.user import User
from server.app.core.limiter import limiter
from server.app.shared.errors import AccountError, ClientError, ConflictError, ValidationError

# PyInstaller 打包后 sys._MEIPASS 指向解压目录
# 开发模式下从当前文件路径（server/app/main.py）上溯到项目根目录
_BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent.parent))
WEB_DIST_DIR = str(_BASE_DIR / "web" / "dist")


def create_app() -> FastAPI:
    settings = get_settings()
    if not settings.jwt_secret:
        raise RuntimeError(
            "GEO_JWT_SECRET is not set. Set it to a long random string before starting the server."
        )

    # 确保数据目录存在（assets/ browser_states/ logs/ exports/）
    ensure_data_dirs()

    # 注册所有平台驱动（import 触发 register() 副作用）
    import server.app.modules.tasks.drivers.toutiao  # noqa: F401

    app = FastAPI(
        title="Geo Collab API",
        version="0.1.0",
        json_encoders={
            datetime: lambda dt: dt.isoformat() + ("Z" if dt.tzinfo is None else "")
        },
    )
    # CORS 仅允许本地开发服务器（桌面应用无跨域风险）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-Geo-Token"],
    )
    # 速率限制
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    # 启动时恢复卡住的记录（上次运行时 crash 导致 status='running' 的记录）
    from server.app.db.session import SessionLocal
    from server.app.modules.tasks import recover_stuck_records
    try:
        recover_db = SessionLocal()
        try:
            recover_stuck_records(recover_db)
        finally:
            recover_db.close()
    except Exception:
        import logging as _logging
        _logging.getLogger(__name__).exception(
            "Startup recovery failed — stuck records may not have been reset"
        )

    # 全局异常处理：业务层统一 raise ClientError → 400
    @app.exception_handler(ClientError)
    async def _client_error_handler(request: Request, exc: ClientError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # ConflictError(ValueError) 有更具体的含义 → 409，优先于 ValueError 处理器
    @app.exception_handler(ConflictError)
    async def _conflict_error_handler(request: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _validation_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(AccountError)
    async def _account_error_handler(request: Request, exc: AccountError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        import logging as _logging
        _logging.getLogger(__name__).exception(
            "Unhandled exception at %s %s", request.method, request.url.path
        )
        return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})

    # 无鉴权端点：前端启动时判断是否需要初始化管理员
    @app.get("/api/bootstrap", include_in_schema=False)
    async def bootstrap() -> dict:
        from server.app.db.session import SessionLocal

        db = SessionLocal()
        try:
            if db.query(User).first() is None:
                return {"needs_setup": True}
            return {"authenticated": False}
        finally:
            db.close()

    # 注册 auth 路由（不加鉴权依赖）
    app.include_router(auth_router, prefix="/api/auth", tags=["auth"])

    # 注册 API 路由模块（全部需要 JWT cookie 鉴权）
    app.include_router(accounts_router, prefix="/api/accounts", tags=["accounts"], dependencies=[Depends(get_current_user)])
    app.include_router(article_groups_router, prefix="/api/article-groups", tags=["article-groups"], dependencies=[Depends(get_current_user)])
    app.include_router(articles_router, prefix="/api/articles", tags=["articles"], dependencies=[Depends(get_current_user)])
    app.include_router(assets_router, prefix="/api/assets", tags=["assets"], dependencies=[Depends(get_current_user)])
    app.include_router(chunked_assets_router, prefix="/api/chunked-assets", tags=["chunked-assets"], dependencies=[Depends(get_current_user)])
    app.include_router(publish_records_router, prefix="/api/publish-records", tags=["publish-records"], dependencies=[Depends(get_current_user)])
    app.include_router(system_router, prefix="/api/system", tags=["system"], dependencies=[Depends(get_current_user)])
    app.include_router(tasks_router, prefix="/api/tasks", tags=["tasks"], dependencies=[Depends(get_current_user)])

    try:
        # 挂载前端静态文件（Vite 构建产物）
        app.mount("/assets", StaticFiles(directory=f"{WEB_DIST_DIR}/assets"), name="web-assets")

        # SPA 兜底路由：所有非 API 路径返回 index.html
        @app.get("/{full_path:path}", include_in_schema=False)
        def serve_web_app(full_path: str) -> FileResponse:
            if full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="接口不存在")
            return FileResponse(f"{WEB_DIST_DIR}/index.html")

    except RuntimeError:
        # 开发模式下 static 目录可能不存在，静默跳过
        pass

    return app


# 模块级 app 实例，uvicorn 直接引用
app = create_app()
