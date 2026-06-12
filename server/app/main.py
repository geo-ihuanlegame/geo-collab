"""
Geo 协作 API 应用工厂与全局配置。

入口点：
  - 开发: uvicorn server.app.main:app --reload
  - 生产: docker-compose up（Dockerfile 的 CMD 先跑 alembic upgrade head）

阅读顺序建议：
  1. create_app() → 了解路由注册、全局异常处理、启动行为
  2. modules/tasks/models.py → PublishTask / PublishRecord 状态机
  3. modules/tasks/executor.py → 任务执行引擎
  4. modules/tasks/drivers/toutiao.py → 头条浏览器自动化
"""

import sys
from datetime import datetime
from pathlib import Path

# ── datetime 序列化补丁 ──
# 在 Pydantic 模型定义之前安装，确保所有无时区 datetime 输出带 "Z" 后缀
# 这样前端 new Date("2026-05-12T14:00:00Z") 能正确识别为 UTC
# 涉及三个层级：Pydantic 模型、FastAPI 内置编码器、FastAPI 构造函数
from pydantic import BaseModel

# ── datetime 序列化：在 BaseModel.model_config 上设置，所有子类自动继承 ──
BaseModel.model_config["json_encoders"] = {
    datetime: lambda dt: dt.isoformat() + ("Z" if dt.tzinfo is None else ""),
}

import fastapi.encoders

fastapi.encoders.ENCODERS_BY_TYPE[datetime] = lambda dt: (
    dt.isoformat() + ("Z" if dt.tzinfo is None else "")
)

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from server.app.core.config import get_settings
from server.app.core.limiter import limiter
from server.app.core.paths import ensure_data_dirs
from server.app.core.security import get_current_user
from server.app.modules.accounts.router import router as accounts_router
from server.app.modules.ai_generation.router import router as generation_router
from server.app.modules.ai_generation.scheme_router import scheme_router
from server.app.modules.articles.router import (
    article_groups_router,
    articles_router,
    assets_router,
    chunked_assets_router,
)
from server.app.modules.audit.router import router as audit_router
from server.app.modules.hot_lists.router import router as hot_lists_router
from server.app.modules.image_library.router import files_router as stock_files_router
from server.app.modules.image_library.router import router as stock_images_router
from server.app.modules.pipelines.router import router as pipelines_router
from server.app.modules.prompt_templates.router import router as prompt_templates_router
from server.app.modules.system.auth_router import router as auth_router
from server.app.modules.system.models import User
from server.app.modules.system.system_router import router as system_router
from server.app.modules.system.users_router import router as users_router
from server.app.modules.tasks.router import publish_records_router, tasks_router
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

    # 导入触发注册副作用：pipelines 节点类型 + 所有平台驱动
    # 驱动注册集中在 drivers.bootstrap，web 进程与发布 worker 共用同一份，避免漂移（见该模块注释）。
    import server.app.modules.pipelines.nodes  # noqa: F401
    import server.app.modules.tasks.drivers.bootstrap  # noqa: F401

    app = FastAPI(
        title="Geo Collab API",
        version="0.1.0",
        json_encoders={datetime: lambda dt: dt.isoformat() + ("Z" if dt.tzinfo is None else "")},
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
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]  # slowapi 的处理器按自身异常类型标注
    # 启动时恢复卡住的记录（上次运行时崩溃导致 status='running' 的记录）
    # 多实例部署时只允许一个实例执行恢复，其他实例设 GEO_RUN_STARTUP_RECOVERY=false
    from server.app.db.session import SessionLocal
    from server.app.modules.tasks import recover_stuck_records

    if get_settings().run_startup_recovery:
        try:
            recover_db = SessionLocal()
            try:
                recover_stuck_records(recover_db)
                from server.app.modules.pipelines.recovery import recover_stuck_pipeline_runs

                recover_stuck_pipeline_runs(recover_db)
                from server.app.modules.ai_generation.scheme_executor import (
                    recover_stuck_scheme_runs,
                )

                recover_stuck_scheme_runs(recover_db)
            finally:
                recover_db.close()
        except Exception:
            import logging as _logging

            _logging.getLogger(__name__).exception(
                "Startup recovery failed — stuck records may not have been reset"
            )

    # 全局异常处理：子类先注册，父类后注册，确保子类不被父类处理器吃掉
    # ConflictError → 409（子类，先注册）
    @app.exception_handler(ConflictError)
    async def _conflict_error_handler(request: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _validation_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(AccountError)
    async def _account_error_handler(request: Request, exc: AccountError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # ClientError → 400（父类，后注册）
    @app.exception_handler(ClientError)
    async def _client_error_handler(request: Request, exc: ClientError) -> JSONResponse:
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

    # 注册认证路由（不加鉴权依赖）
    app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
    app.include_router(users_router, prefix="/api/users", tags=["users"])

    # 注册 API 路由模块（全部需要 JWT cookie 鉴权）
    app.include_router(
        accounts_router,
        prefix="/api/accounts",
        tags=["accounts"],
        dependencies=[Depends(get_current_user)],
    )
    app.include_router(
        article_groups_router,
        prefix="/api/article-groups",
        tags=["article-groups"],
        dependencies=[Depends(get_current_user)],
    )
    app.include_router(
        articles_router,
        prefix="/api/articles",
        tags=["articles"],
        dependencies=[Depends(get_current_user)],
    )
    app.include_router(
        assets_router,
        prefix="/api/assets",
        tags=["assets"],
        dependencies=[Depends(get_current_user)],
    )
    app.include_router(
        chunked_assets_router,
        prefix="/api/chunked-assets",
        tags=["chunked-assets"],
        dependencies=[Depends(get_current_user)],
    )
    app.include_router(
        publish_records_router,
        prefix="/api/publish-records",
        tags=["publish-records"],
        dependencies=[Depends(get_current_user)],
    )
    app.include_router(
        system_router,
        prefix="/api/system",
        tags=["system"],
        dependencies=[Depends(get_current_user)],
    )
    app.include_router(
        tasks_router, prefix="/api/tasks", tags=["tasks"], dependencies=[Depends(get_current_user)]
    )
    app.include_router(
        prompt_templates_router,
        prefix="/api/prompt-templates",
        tags=["prompt-templates"],
        dependencies=[Depends(get_current_user)],
    )
    app.include_router(
        generation_router,
        prefix="/api/generation",
        tags=["generation"],
        dependencies=[Depends(get_current_user)],
    )
    app.include_router(
        scheme_router,
        prefix="/api/generation",
        tags=["generation-schemes"],
        dependencies=[Depends(get_current_user)],
    )
    app.include_router(
        pipelines_router,
        prefix="/api/pipelines",
        tags=["pipelines"],
        dependencies=[Depends(get_current_user)],
    )
    app.include_router(
        stock_images_router,
        prefix="/api/image-library",
        tags=["image-library"],
        dependencies=[Depends(get_current_user)],
    )
    app.include_router(stock_files_router, prefix="/api/stock-images", tags=["stock-images"])
    app.include_router(
        audit_router,
        prefix="/api/audit-logs",
        tags=["audit-logs"],
        dependencies=[Depends(get_current_user)],
    )
    app.include_router(
        hot_lists_router,
        prefix="/api/hot-lists",
        tags=["hot-lists"],
        dependencies=[Depends(get_current_user)],
    )

    # 为方案运行后台线程提供 SessionLocal（generation 没有独立工作进程，靠路由内后台线程执行）
    import server.app.modules.ai_generation.scheme_router as _scheme_routes

    _scheme_routes.bg_session_factory = SessionLocal

    # 为 pipelines 运行后台线程提供 SessionLocal（同样靠路由内后台线程执行）
    import server.app.modules.pipelines.router as _pipelines_routes

    _pipelines_routes.bg_session_factory = SessionLocal

    # 问题池定时镜像同步：仅在 GEO_QUESTION_POOL_AUTO_SYNC_ENABLED=true 时启动后台线程。
    # 默认关闭，测试 / 本地不会打真实飞书。启动失败只记日志，不致命。
    try:
        from server.app.modules.ai_generation.sync_scheduler import start_auto_sync

        start_auto_sync(SessionLocal)
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).exception("Failed to start question-pool auto-sync thread")

    try:
        from server.app.modules.pipelines.scheduler import start_pipeline_scheduler

        if get_settings().pipeline_scheduler_enabled:
            start_pipeline_scheduler(SessionLocal)
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).exception("start_pipeline_scheduler failed")

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
        # 开发模式下静态目录可能不存在，静默跳过
        pass

    return app


# 模块级 app 实例，uvicorn 直接引用
app = create_app()
