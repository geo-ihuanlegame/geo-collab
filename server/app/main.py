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
from server.app.modules.ai_generation.router import mcp_router as generation_mcp_router
from server.app.modules.ai_generation.router import router as generation_router
from server.app.modules.ai_generation.scheme_router import scheme_router
from server.app.modules.ai_models.router import router as ai_models_router
from server.app.modules.articles.router import (
    article_groups_router,
    articles_mcp_router,
    articles_router,
    assets_router,
    chunked_assets_router,
)
from server.app.modules.audit.router import router as audit_router
from server.app.modules.auto_review.router import router as auto_review_router
from server.app.modules.hot_lists.router import router as hot_lists_router
from server.app.modules.image_library.router import files_router as stock_files_router
from server.app.modules.image_library.router import router as stock_images_router
from server.app.modules.mcp_catalog.connect_router import (
    mcp_connect_health_router,
    mcp_connect_user_router,
)
from server.app.modules.mcp_catalog.router import router as mcp_catalog_router
from server.app.modules.performance.router import router as performance_router
from server.app.modules.pipelines.router import router as pipelines_router
from server.app.modules.prompt_templates.router import router as prompt_templates_router
from server.app.modules.system.auth_router import router as auth_router
from server.app.modules.system.models import User
from server.app.modules.system.system_router import mcp_system_router
from server.app.modules.system.system_router import router as system_router
from server.app.modules.system.users_router import router as users_router
from server.app.modules.tasks.router import publish_records_router, tasks_mcp_router, tasks_router
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
        allow_headers=["Content-Type", "X-Geo-Token", "X-MCP-Token"],
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

    # 启动时记录加密密钥配置状态（spec §4.1）
    import logging as _logging

    from server.app.core.crypto import encryption_enabled

    _startup_logger = _logging.getLogger(__name__)
    if encryption_enabled():
        _startup_logger.info("敏感凭据静态加密已启用（GEO_SECRET_KEY 已配置）")
    else:
        _startup_logger.warning(
            "敏感凭据静态加密未启用：未配置 GEO_SECRET_KEY/GEO_SECRET_KEYS，账号凭据将以明文存储"
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

    # MCP 服务对服务路由（MCP token 鉴权，不走 user JWT cookie）
    # mcp_catalog：跨模块的只读 list / get 端点，路径在 /api/mcp/ 下避免与 user-JWT 路由冲突
    app.include_router(
        mcp_catalog_router,
        prefix="/api/mcp",
        tags=["mcp-catalog"],
    )
    # MCP 接入指引（前端「MCP 接入」tab 用）
    # user JWT 鉴权（与 system_router 等 user-JWT 路由同一组依赖）
    app.include_router(
        mcp_connect_user_router,
        prefix="/api/mcp",
        tags=["mcp-connect"],
        dependencies=[Depends(get_current_user)],
    )
    # MCP token 鉴权（router 自带 dependency）
    app.include_router(
        mcp_connect_health_router,
        prefix="/api/mcp",
        tags=["mcp-connect"],
    )
    app.include_router(
        generation_mcp_router,
        prefix="/api/generation",
        tags=["generation-mcp"],
    )
    app.include_router(
        articles_mcp_router,
        prefix="/api/articles",
        tags=["articles-mcp"],
        # 不挂 get_current_user — MCP token 在 endpoint 内单独校验
    )
    # auto_review 走 /api/articles 前缀（与现有 article 路由同前缀，由 MCP token 单独鉴权）
    app.include_router(
        auto_review_router,
        prefix="/api/articles",
        tags=["auto-review"],
    )
    # performance 路由：MCP token 鉴权，不走 user JWT cookie
    # 路由内路径绝对写出（/prompt-templates/…, /accounts/…, /publish-records/…），
    # prefix=/api 后解析为 /api/prompt-templates/.../performance 等
    app.include_router(
        performance_router,
        prefix="/api",
        tags=["performance"],
    )

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
        mcp_system_router,
        prefix="/api/system",
        tags=["system-mcp"],
        # 不挂 get_current_user 依赖，MCP token 在 endpoint 内单独校验
    )
    app.include_router(
        tasks_router, prefix="/api/tasks", tags=["tasks"], dependencies=[Depends(get_current_user)]
    )
    app.include_router(
        tasks_mcp_router,
        prefix="/api/tasks",
        tags=["tasks-mcp"],
        # 不挂 get_current_user — MCP token 在 endpoint 内单独校验
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
    app.include_router(
        ai_models_router,
        prefix="/api/ai-models",
        tags=["ai-models"],
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

    # TapTap cookie 体检：GEO_TAPTAP_COOKIE_CHECK_ENABLED=true 时启动后台线程，纯 HTTP 探
    # account-profile/v1/me，失效则置 expired + 飞书喊人重登（不自动登录）。失败只记日志、不致命。
    try:
        from server.app.modules.tasks.taptap_health import start_cookie_check

        start_cookie_check(SessionLocal)
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).exception("start_cookie_check failed")

    # 资源指标周期采样（Task 3，封堵 #10）：后台守护线程每 N 秒采池/run 快照打点 +
    # checked_out/max 超阈值升 WARNING（走 resource_metrics.emit_resource_alert 统一告警 hook，
    # Task 5 后续接同一通道）。开关 GEO_RESOURCE_METRICS_SAMPLING_ENABLED 默认开、可关。
    # 线程 daemon + 每轮 try/except，启动失败只记日志、不阻塞 create_app。
    try:
        from server.app.shared.resource_metrics import start_resource_sampler

        start_resource_sampler(SessionLocal)
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).exception("start_resource_sampler failed")

    # 连接预算断言（Task 5，封堵 #4）：启动期一次性核对 anyio 线程池 + 发布并发封顶 + 安全余量
    # 是否 ≤ 连接池容量；越界经 resource_metrics 统一告警 hook 提示扩池/降发布并发（绝不缩 anyio）。
    # check_connection_budget 内部已吞异常、绝不抛——这里的 try/except 仅为双保险。
    try:
        from server.app.shared.resource_metrics import check_connection_budget

        check_connection_budget()
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).exception("check_connection_budget failed")

    # AI 模型注册表首次播种：表为空时从 GEO_AI_ENGINES + 格式默认模型建初始行（幂等）。
    # 密钥不入库；启动失败只记日志、不阻塞 create_app。
    try:
        from server.app.modules.ai_models.service import seed_ai_models_if_empty

        with SessionLocal() as _seed_db:
            seed_ai_models_if_empty(_seed_db)
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).warning("ai_models seed skipped", exc_info=True)

    # ── MCP HTTP transport mount ────────────────────────────────────────────
    # FastMCP 的 streamable_http_app() mount 到 /mcp,用户端 ~/.claude.json 只需配 url + token,
    # 无须本地装 Python / clone 仓库 / 设 PYTHONPATH。鉴权走 McpTokenMiddleware (复用
    # require_mcp_token 的 hmac compare_digest 逻辑)。
    # **必须**挂在 SPA fallback `@app.get("/{full_path:path}")` 之前 —— 否则非 /api/ 路径
    # 全部被 fallback 兜住,/mcp 永远 404。
    try:
        import contextlib

        from server.app.core.mcp_auth import McpTokenMiddleware
        from server.mcp.server import build_http_app, mcp

        mcp_app = build_http_app()
        mcp_app.add_middleware(McpTokenMiddleware)
        app.mount("/mcp", mcp_app)

        # StreamableHTTPSessionManager.handle_request() checks `_task_group is not None`
        # unconditionally (even in stateless mode) — the task group is initialized by the
        # session manager's own lifespan context (`session_manager.run()`). Mounted sub-apps
        # do NOT trigger their own lifespan in FastAPI/Starlette, so we hook it into
        # the outer FastAPI app's lifespan here instead.
        _mcp_session_manager = mcp.session_manager

        @contextlib.asynccontextmanager
        async def _lifespan_with_mcp(application):  # type: ignore[misc]
            async with _mcp_session_manager.run():
                yield

        # 注意：这是一次性赋值，会覆盖任何已设置的 lifespan。如果后续要再加 startup hook,
        # 不要再赋值 lifespan_context —— 改为在这里用 contextlib.AsyncExitStack 合并多个 lifespan,
        # 否则会静默丢掉 MCP session_manager 的 task group, 认证后请求会 500。
        app.router.lifespan_context = _lifespan_with_mcp

    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).exception("MCP HTTP mount failed — /mcp endpoint disabled")

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
