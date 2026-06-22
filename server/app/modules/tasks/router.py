"""任务模块路由。"""

import logging
import threading
import time
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy import update as _upd
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.app.core.security import get_current_user
from server.app.db.session import get_db
from server.app.modules.audit.service import add_audit_entry
from server.app.modules.system.models import User, WorkerHeartbeat
from server.app.modules.tasks import (
    TERMINAL_TASK_STATUSES,
    cancel_task,
    create_task,
    execute_task,
    get_record,
    get_task,
    list_task_logs,
    list_task_records,
    list_tasks,
    manual_confirm_record,
    preview_task_assignment,
    resolve_user_input_record,
    retry_record,
)
from server.app.modules.tasks.models import PublishRecord, PublishTask
from server.app.modules.tasks.schemas import (
    AutoDistributeRequest,
    AutoDistributeResponse,
    ManualConfirmInput,
    PublishRecordRead,
    TaskAccountInput,
    TaskAssignmentPreviewRead,
    TaskCreate,
    TaskLogRead,
    TaskRead,
    to_log_read,
    to_record_read,
    to_task_read,
)
from server.app.shared.errors import AccountError, ValidationError

tasks_router = APIRouter()
publish_records_router = APIRouter()

# 后台任务使用的 Session 工厂（测试时可替换为 TestingSessionLocal）
bg_session_factory: Any = None

# 是否允许本进程内联执行发布（封堵 #6）。默认 False：生产 web 进程绝不在自己进程里起浏览器发布，
# 只入队 pending、由单实例 worker 抢占（否则 web + worker 双进程各自 ×N，_global_publish_gate 失效）。
# 仅测试 / 单机 dev 通过显式置 True（见 build_test_app）才内联执行。
inline_execute_enabled: bool = False

# worker 新鲜度窗口（秒）：与 system_router 的 worker_online 判定一致。超此窗口无心跳即视为无活跃 worker。
WORKER_FRESH_WINDOW_SECONDS = 30


def _inline_execute_active() -> bool:
    """是否在本进程内联执行发布：仅显式开关开启且已注入 bg_session_factory 时为真。

    生产 web 进程两者皆否 → 只入队、交由单实例 worker 抢占（封堵 #6 的双进程各 ×N）。
    """
    return inline_execute_enabled and bg_session_factory is not None


def _has_fresh_worker(db: Session) -> bool:
    """是否存在新鲜（WORKER_FRESH_WINDOW_SECONDS 内有心跳）的发布 worker。"""
    from server.app.core.time import utcnow as _utcnow

    return bool(
        db.scalar(
            select(func.count())
            .select_from(WorkerHeartbeat)
            .where(
                WorkerHeartbeat.heartbeat_at
                >= _utcnow() - timedelta(seconds=WORKER_FRESH_WINDOW_SECONDS)
            )
        )
    )


# ── 任务辅助函数 ────────────────────────────────────────────────────────────


def _verify_task_ownership(task: PublishTask | None, current_user: User) -> PublishTask:
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if current_user.role != "admin" and task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


# ── 任务路由 ────────────────────────────────────────────────────────────────


@tasks_router.get("", response_model=list[TaskRead])
def read_tasks(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TaskRead]:
    user_id_filter = None if current_user.role == "admin" else current_user.id
    tasks = list_tasks(db, skip=skip, limit=limit, user_id=user_id_filter)
    return [to_task_read(task) for task in tasks]


@tasks_router.post("", response_model=TaskRead)
def create_task_endpoint(
    payload: TaskCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskRead:
    try:
        new_task = create_task(db, current_user.id, payload, role=current_user.role)
        add_audit_entry(
            db,
            user=current_user,
            action="task.create",
            target_type="task",
            target_id=new_task.id,
            payload={
                "name": getattr(payload, "name", None),
                "platform_code": getattr(payload, "platform_code", None),
                "account_ids": list(getattr(payload, "account_ids", []) or []),
            },
            request=request,
        )
        return to_task_read(new_task)
    except IntegrityError as exc:
        db.rollback()
        if payload.client_request_id:
            existing = db.execute(
                select(PublishTask).where(
                    PublishTask.client_request_id == payload.client_request_id,
                    PublishTask.user_id == current_user.id,
                    PublishTask.is_deleted == False,  # noqa: E712
                )
            ).scalar_one_or_none()
            if existing is not None:
                # 幂等重试：并发请求已经创建了这个任务。
                refreshed = get_task(db, existing.id)
                return to_task_read(refreshed or existing)
        # 上面的幂等查询无法消解的 IntegrityError 都是真实约束冲突；
        # 明确抛 409，避免隐式 return None 被序列化成不透明的 500。
        raise HTTPException(
            status_code=409, detail="请求冲突：client_request_id 已存在或数据完整性约束失败"
        ) from exc


def _group_accounts_by_platform(db: Session, account_ids: list[int]) -> list[tuple[str, list[int]]]:
    """把选中账号按其真实平台分组（去重、保留传入顺序）。

    返回 [(platform_code, [account_id, ...]), ...]，平台按 code 升序、组内保留传入顺序，
    便于稳定的 round-robin 与可测性。任一账号不存在/已删除即抛 AccountError（与 create_task 一致 → 400）。
    """
    from server.app.modules.accounts.models import Account
    from server.app.modules.system.models import Platform

    seen: set[int] = set()
    ordered: list[int] = []
    for account_id in account_ids:
        if account_id not in seen:
            seen.add(account_id)
            ordered.append(account_id)

    rows = db.execute(
        select(Account.id, Platform.code)
        .join(Platform, Account.platform_id == Platform.id)
        .where(Account.id.in_(ordered), Account.is_deleted == False)  # noqa: E712
    ).all()
    platform_by_account = {account_id: code for account_id, code in rows}
    missing = [account_id for account_id in ordered if account_id not in platform_by_account]
    if missing:
        raise AccountError(f"Account not found: {missing[0]}")

    grouped: dict[str, list[int]] = {}
    for account_id in ordered:
        grouped.setdefault(platform_by_account[account_id], []).append(account_id)
    return [(code, grouped[code]) for code in sorted(grouped)]


@tasks_router.post("/auto-distribute", response_model=AutoDistributeResponse)
def auto_distribute_endpoint(
    payload: AutoDistributeRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AutoDistributeResponse:
    """一键自动分发：把「文章/分组 + 账号列表」简化请求映射成发布任务并立即后台执行。

    选中账号按其真实平台分组，每个平台各建一个任务（受『一个任务=单平台』约束，
    见 service.py:_validated_accounts）。单文章→single（每平台取首个账号，与前端轮询预览
    『单篇 → 首账号』一致），分组→group_round_robin。审核门禁/账号校验都在 create_task 内
    （抛命名异常→400）。
    """
    is_group = payload.group_id is not None
    target_label = f"分组 {payload.group_id}" if is_group else f"文章 {payload.article_id}"

    groups = _group_accounts_by_platform(db, list(payload.account_ids))
    if not groups:
        raise ValidationError("At least one account is required")

    base_name = payload.name or f"自动分发 {target_label}"
    multi = len(groups) > 1
    created_tasks: list[PublishTask] = []
    for platform_code, account_ids in groups:
        # 单文章每平台只发到首个账号（单篇 round-robin 必然落在首账号）；分组保留全部账号做轮询。
        used_account_ids = account_ids if is_group else account_ids[:1]
        accounts = [
            TaskAccountInput(account_id=account_id, sort_order=index)
            for index, account_id in enumerate(used_account_ids)
        ]
        name = f"{base_name} · {platform_code}" if multi else base_name
        task_create = TaskCreate(
            name=name,
            task_type="group_round_robin" if is_group else "single",
            article_id=payload.article_id,
            group_id=payload.group_id,
            platform_code=platform_code,
            accounts=accounts,
            stop_before_publish=False,
        )
        # 审核门禁 + 账号有效性校验都在 create_task 内部执行（抛命名异常 → 全局映射 400）。
        new_task = create_task(db, current_user.id, task_create, role=current_user.role)
        created_tasks.append(new_task)

    # 先提交，保证后台执行线程的独立 session 能看到这些任务。
    db.commit()

    for new_task in created_tasks:
        add_audit_entry(
            db,
            user=current_user,
            action="task.auto_distribute",
            target_type="task",
            target_id=new_task.id,
            payload={
                "name": new_task.name,
                "article_id": payload.article_id,
                "group_id": payload.group_id,
                "account_ids": [account.account_id for account in new_task.accounts],
            },
            request=request,
        )

    # 触发后台执行（与 POST /api/tasks/{id}/execute 同一条懒加载 bg_session_factory 路径）。
    for new_task in created_tasks:
        _start_background_execute(new_task.id)

    return AutoDistributeResponse(tasks=[to_task_read(task) for task in created_tasks])


@tasks_router.post("/preview", response_model=TaskAssignmentPreviewRead)
def preview_task_assignment_endpoint(
    payload: TaskCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskAssignmentPreviewRead:
    return preview_task_assignment(db, payload, user_id=current_user.id, role=current_user.role)


class _ExecuteResponse(BaseModel):
    queued: bool
    # 生产入队路径：是否有新鲜 worker（无则任务会卡 pending，前端可据此提示）；None=内联执行无需 worker。
    worker_online: bool | None = None


@tasks_router.post("/{task_id}/execute", status_code=202, response_model=_ExecuteResponse)
def start_task_execution(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> _ExecuteResponse:
    """触发任务执行，立即返回 202。

    两条路径（封堵 #6）：
    - 显式开关开启（_inline_execute_active：测试/单机 dev）→ 直接起后台线程跑 execute_task。
    - 否则（生产 web）→ **不在 web 进程发布**，只释放陈旧 worker 认领、留 pending，交由单实例
      worker 抢占；并查 worker 新鲜度，无活跃 worker 时告警 + 回包 worker_online=False，
      避免任务静默卡 pending。
    """
    from server.app.core.time import utcnow as _utcnow

    task = _verify_task_ownership(get_task(db, task_id), current_user)
    if task.status in TERMINAL_TASK_STATUSES:
        raise HTTPException(status_code=409, detail=f"Task is already terminal: {task.status}")

    worker_online: bool | None = None
    if _inline_execute_active():
        # 测试 / 单机 dev（显式开关）：立即在后台线程执行
        def _run() -> None:
            bg_db = bg_session_factory()
            try:
                bg_task = get_task(bg_db, task_id)
                if bg_task:
                    execute_task(bg_db, bg_task)
                bg_db.commit()
            except Exception:
                bg_db.rollback()
                logging.getLogger(__name__).exception("Background task %s failed", task_id)
            finally:
                bg_db.close()

        threading.Thread(target=_run, daemon=True).start()
    else:
        # 生产模式：web 进程不发布。释放陈旧 worker 认领后留 pending，由独立 worker 轮询捡走。
        db.execute(
            select(PublishTask).where(PublishTask.id == task_id)  # 重新加锁用于更新
        )
        db.execute(
            _upd(PublishTask)
            .where(
                PublishTask.id == task_id,
                (PublishTask.worker_lease_until < _utcnow()) | PublishTask.worker_id.is_(None),
            )
            .values(worker_id=None, worker_lease_until=None, worker_heartbeat_at=None)
        )
        db.commit()
        # 无新鲜 worker → 任务会静默卡 pending：走统一告警 hook + 回包提示。
        worker_online = _has_fresh_worker(db)
        if not worker_online:
            from server.app.shared.resource_metrics import emit_resource_alert

            emit_resource_alert(
                f"task {task_id} enqueued but no fresh publish worker "
                f"(no WorkerHeartbeat within {WORKER_FRESH_WINDOW_SECONDS}s); "
                "it will sit pending until a worker comes online",
                {"task_id": task_id},
            )

    add_audit_entry(
        db,
        user=current_user,
        action="task.execute.start",
        target_type="task",
        target_id=task_id,
        payload={"stop_before_publish": task.stop_before_publish},
        request=request,
    )
    return _ExecuteResponse(queued=True, worker_online=worker_online)


@tasks_router.post("/{task_id}/cancel", response_model=TaskRead)
def cancel_existing_task(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskRead:
    task = _verify_task_ownership(get_task(db, task_id), current_user)
    cancelled = cancel_task(db, task)
    add_audit_entry(
        db,
        user=current_user,
        action="task.cancel",
        target_type="task",
        target_id=task_id,
        payload=None,
        request=request,
    )
    return to_task_read(cancelled)


@tasks_router.get("/{task_id}/logs", response_model=list[TaskLogRead])
def read_task_logs(
    task_id: int,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=100, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TaskLogRead]:
    _verify_task_ownership(get_task(db, task_id), current_user)
    return [to_log_read(log) for log in list_task_logs(db, task_id, after_id=after_id, limit=limit)]


@tasks_router.get("/{task_id}", response_model=TaskRead)
def read_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TaskRead:
    task = _verify_task_ownership(get_task(db, task_id), current_user)
    return to_task_read(task)


@tasks_router.get("/{task_id}/records", response_model=list[PublishRecordRead])
def read_task_records(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[PublishRecordRead]:
    _verify_task_ownership(get_task(db, task_id), current_user)
    return [to_record_read(record) for record in list_task_records(db, task_id)]


@tasks_router.get("/{task_id}/stream")
def stream_task_events(
    task_id: int,
    after_log_id: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """SSE 推送任务执行进度：每秒轮询，task/records 变化才推（diff 去抖），增量推 log。

    用独立 SessionLocal（不复用请求 db，连接要长持）；每轮 expire_all 强制读最新；task 到终态推 done 收流。
    """
    from server.app.db.session import SessionLocal as _SL

    check_db = _SL()
    try:
        _verify_task_ownership(get_task(check_db, task_id), current_user)
    finally:
        check_db.close()

    def _generate():
        from server.app.db.session import SessionLocal as _SL2

        last_id = after_log_id
        prev_records = ""
        prev_task = ""

        try:
            while True:
                # 每个轮询周期开一条独立 session、查完即关：连接只在查询的几毫秒被借走，
                # time.sleep(1) 期间归还连接池。避免 SSE 长连接钉死池连接、多人观看时打满
                # 连接池拖垮全站（连接池耗尽事故主因）。事件先序列化成字符串，close 后再 yield，
                # 不会在连接归还后触发 ORM 懒加载。
                events: list[str] = []
                done = False
                sess = _SL2()
                try:
                    task = get_task(sess, task_id)
                    if task is None:
                        events.append("event: done\ndata: {}\n\n")
                        done = True
                    else:
                        task_json = to_task_read(task).model_dump_json()
                        if task_json != prev_task:
                            events.append(f"event: task\ndata: {task_json}\n\n")
                            prev_task = task_json

                        new_logs = list_task_logs(sess, task_id, after_id=last_id, limit=100)
                        for log in new_logs:
                            events.append(
                                f"event: log\ndata: {to_log_read(log).model_dump_json()}\n\n"
                            )
                        if new_logs:
                            last_id = max(log.id for log in new_logs)

                        records = list_task_records(sess, task_id)
                        records_json = (
                            "["
                            + ",".join(to_record_read(r).model_dump_json() for r in records)
                            + "]"
                        )
                        if records_json != prev_records:
                            events.append(f"event: records\ndata: {records_json}\n\n")
                            prev_records = records_json

                        if task.status in TERMINAL_TASK_STATUSES:
                            events.append("event: done\ndata: {}\n\n")
                            done = True
                finally:
                    sess.close()  # 关键：yield / sleep 之前就把连接还给池

                yield from events
                if done:
                    break
                time.sleep(1)
        except GeneratorExit:
            return
        except Exception:
            logging.getLogger(__name__).exception("SSE error for task %s", task_id)
            try:
                yield "retry: 15000\nevent: error\ndata: {}\n\n"
            except Exception:
                pass

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 发布记录辅助函数 ────────────────────────────────────────────────────────


def _verify_record_ownership(
    record: PublishRecord | None, current_user: User, db: Session
) -> PublishRecord:
    if record is None:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    task = get_task(db, record.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    if current_user.role != "admin" and task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    return record


def _start_background_execute(task_id: int) -> None:
    """用户操作（手动确认/重试/解决人工输入/自动分发）后在后台线程续跑任务。

    仅在显式开关开启时生效（测试/单机 dev）；生产模式 no-op，交给独立 worker 轮询（封堵 #6）。
    后台线程自建并自管 session（与请求线程的 db 隔离，session 非线程安全）。
    """
    if not _inline_execute_active():
        # 生产模式：worker 发现 pending 记录时自行捡起任务。
        return

    def _run() -> None:
        bg_db = bg_session_factory()
        try:
            bg_task = get_task(bg_db, task_id)
            if bg_task:
                execute_task(bg_db, bg_task)
            bg_db.commit()
        except Exception:
            bg_db.rollback()
            logging.getLogger(__name__).exception(
                "Background execute after user action failed for task %s", task_id
            )
        finally:
            bg_db.close()

    threading.Thread(target=_run, daemon=True).start()


# ── 发布记录路由 ────────────────────────────────────────────────────────────


@publish_records_router.post("/{record_id}/manual-confirm", response_model=PublishRecordRead)
def manual_confirm_record_endpoint(
    record_id: int,
    payload: ManualConfirmInput,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PublishRecordRead:
    record = _verify_record_ownership(get_record(db, record_id), current_user, db)
    publish_url = str(payload.publish_url) if payload.publish_url else None
    result = manual_confirm_record(db, record, payload.outcome, publish_url, payload.error_message)
    db.commit()

    add_audit_entry(
        db,
        user=current_user,
        action="publish_record.manual_confirm",
        target_type="publish_record",
        target_id=record_id,
        payload=None,
        request=request,
    )

    task = get_task(db, record.task_id)
    if task is not None and task.status not in TERMINAL_TASK_STATUSES:
        _start_background_execute(record.task_id)

    return to_record_read(result)


@publish_records_router.post("/{record_id}/resolve-user-input", response_model=PublishRecordRead)
def resolve_user_input_record_endpoint(
    record_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PublishRecordRead:
    record = _verify_record_ownership(get_record(db, record_id), current_user, db)
    result = resolve_user_input_record(db, record)
    db.commit()

    add_audit_entry(
        db,
        user=current_user,
        action="publish_record.resolve_user_input",
        target_type="publish_record",
        target_id=record_id,
        payload=None,
        request=request,
    )

    task = get_task(db, record.task_id)
    if task is not None and task.status not in TERMINAL_TASK_STATUSES:
        _start_background_execute(record.task_id)

    return to_record_read(result)


@publish_records_router.post("/{record_id}/retry", response_model=PublishRecordRead)
def retry_record_endpoint(
    record_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PublishRecordRead:
    record = _verify_record_ownership(get_record(db, record_id), current_user, db)
    result = retry_record(db, record)
    db.commit()
    add_audit_entry(
        db,
        user=current_user,
        action="publish_record.retry",
        target_type="publish_record",
        target_id=record_id,
        payload={"new_record_id": result.id},
        request=request,
    )
    _start_background_execute(record.task_id)
    return to_record_read(result)


# === MCP-facing endpoints（不走 user JWT，走 MCP token）===

from server.app.core.mcp_auth import require_mcp_token  # noqa: E402

tasks_mcp_router = APIRouter()


class TaskMcpCreatePayload(BaseModel):
    name: str
    article_ids: list[int]
    account_ids: list[int]
    platform_code: str = "toutiao"
    user_id: int  # MCP 调时传 operator user id（与 compose-once 一致）
    stop_before_publish: bool = False


class TaskMcpCreateResponse(BaseModel):
    task_id: int


@tasks_mcp_router.post(
    "/mcp",
    response_model=TaskMcpCreateResponse,
    dependencies=[Depends(require_mcp_token)],
)
def create_task_mcp(
    payload: TaskMcpCreatePayload,
    db: Session = Depends(get_db),
) -> TaskMcpCreateResponse:
    """[MCP] Create an article_round_robin task. Reuses task service.create_task."""
    import uuid

    task = create_task(
        db,
        payload.user_id,
        TaskCreate(
            name=payload.name,
            client_request_id=str(uuid.uuid4()),
            task_type="article_round_robin",
            article_ids=payload.article_ids,
            accounts=[
                TaskAccountInput(account_id=aid, sort_order=i)
                for i, aid in enumerate(payload.account_ids)
            ],
            platform_code=payload.platform_code,
            stop_before_publish=payload.stop_before_publish,
        ),
        role="admin",  # MCP 代表系统调用，跳过归属过滤
    )
    db.commit()
    return TaskMcpCreateResponse(task_id=task.id)
