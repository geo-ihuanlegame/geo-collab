import shutil
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from server.app.core.security import require_admin
from server.app.core.time import utcnow
from server.app.db.session import get_db
from server.app.modules.accounts import remote_browser_runtime_status
from server.app.modules.audit.service import add_audit_entry
from server.app.modules.system.models import User, WorkerHeartbeat
from server.app.modules.system.schemas import SystemStatus
from server.app.shared.system_status import get_system_status

router = APIRouter()

# Chrome 浏览器可能的位置
_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "chrome",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]


# 检测 Chrome 浏览器是否可访问
def _browser_ready() -> bool:
    if any(Path(c).exists() or shutil.which(c) for c in _CHROME_CANDIDATES):
        return True
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).exists()
    except Exception:
        return False


# 获取系统运行状态
@router.get("/status", response_model=SystemStatus)
def read_system_status(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> SystemStatus:
    from server.app.modules.accounts.models import Account, BrowserSession
    from server.app.modules.articles.models import Article
    from server.app.modules.tasks.models import PublishRecord, PublishTask

    base = get_system_status()
    data = base.model_dump()
    try:
        data["article_count"] = (
            db.scalar(
                select(func.count()).select_from(Article).where(Article.is_deleted == False)  # noqa: E712
            )
            or 0
        )
        data["account_count"] = (
            db.scalar(
                select(func.count()).select_from(Account).where(Account.is_deleted == False)  # noqa: E712
            )
            or 0
        )
        data["task_count"] = (
            db.scalar(
                select(func.count()).select_from(PublishTask).where(PublishTask.is_deleted == False)  # noqa: E712
            )
            or 0
        )
        data["pending_task_count"] = (
            db.scalar(
                select(func.count())
                .select_from(PublishTask)
                .where(
                    PublishTask.status.in_(["pending", "running"]),
                    PublishTask.is_deleted == False,  # noqa: E712
                    exists().where(
                        PublishRecord.task_id == PublishTask.id,
                        PublishRecord.status == "pending",
                        PublishRecord.is_deleted == False,  # noqa: E712
                    ),
                )
            )
            or 0
        )
        # 自动清理：仅删除 stop_requested=True 且 1 小时未活动的会话
        # （保护正在运行的发文任务，即使在高并发场景下）
        cutoff = utcnow() - timedelta(hours=1)
        deleted_count = (
            db.query(BrowserSession)
            .filter(
                BrowserSession.stop_requested == True,  # noqa: E712
                BrowserSession.last_activity_at < cutoff,
            )
            .delete()
        )
        db.commit()
        if deleted_count and deleted_count > 0:
            add_audit_entry(
                db,
                user=current_user,
                action="system.browser_session.cleanup",
                target_type="system",
                target_id=None,
                payload={"deleted_count": deleted_count},
                request=request,
            )
        data["active_browser_sessions"] = (
            db.scalar(select(func.count()).select_from(BrowserSession)) or 0
        )
        data["worker_online"] = bool(
            db.scalar(
                select(func.count())
                .select_from(WorkerHeartbeat)
                .where(WorkerHeartbeat.heartbeat_at >= utcnow() - timedelta(seconds=30))
            )
        )
    except Exception:
        # 数据库查询失败时返回 -1
        data["article_count"] = data["account_count"] = data["task_count"] = -1
        data["pending_task_count"] = data["active_browser_sessions"] = -1
        data["worker_online"] = False
    data["browser_ready"] = _browser_ready()
    data["novnc_runtime_ready"] = bool(remote_browser_runtime_status().get("ready"))
    return SystemStatus(**data)
