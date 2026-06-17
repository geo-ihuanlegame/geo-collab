"""资源指标采集（Task 3，封堵 #10：全仓零可观测）。

提供一个依赖轻、可从任意线程安全调用的 `collect_resource_metrics()`，返回一份
快照 dict，覆盖：
- DB 连接池状态（`size` / `checked_out` / `overflow` / `checked_in` / 配置 `max`）；
- 闸占用（`gates`）——已注册 `ObservableGate` 的 in_use/waiting/capacity（pipeline/scheme/publish）；
- 活跃发布记录数 / 过期租约数——仅当调用方传入 `db` Session 时才查（轻量 COUNT），
  否则留占位。`collect_resource_metrics()` 默认不开 session、不引入新依赖。

设计约束：
- 池状态读 `engine.pool` 的计数器，纯内存、线程安全、无 IO。
- 任何采集子项失败都被吞掉（返回占位值），整份采集绝不抛错，供后台采样线程安全调用。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Any]


def _collect_gates() -> list[dict[str, Any]]:
    """汇总所有已注册 ObservableGate 的占用快照（pipeline / scheme / publish）。

    闸在各自模块加载时 register_gate(...)；这里只 import concurrency（无业务依赖、无 IO）。
    失败时返回空 list、不抛错。
    """
    try:
        from server.app.shared.concurrency import registered_gate_snapshots

        return registered_gate_snapshots()
    except Exception:
        logger.exception("collect_resource_metrics: failed to read gate snapshots")
        return []


def _collect_pool() -> dict[str, Any]:
    """读取全局 engine 连接池计数。失败时返回带 error 的占位，不抛错。"""
    try:
        from server.app.db.session import engine

        pool = engine.pool
        # QueuePool 暴露这些方法；其它池类型可能缺，故各自 getattr 兜底。
        size = int(pool.size()) if hasattr(pool, "size") else -1
        checked_out = int(pool.checkedout()) if hasattr(pool, "checkedout") else -1
        overflow = int(pool.overflow()) if hasattr(pool, "overflow") else -1
        checked_in = int(pool.checkedin()) if hasattr(pool, "checkedin") else -1
        # 配置上限 = pool_size + max_overflow。overflow() 返回的是"当前已超出 size 的数量"，
        # 可能为负（池尚未填满），不能直接当上限。从 engine 配置读 max_overflow 更可靠。
        max_overflow = int(getattr(pool, "_max_overflow", 0) or 0)
        configured_max = size + max_overflow if size >= 0 and max_overflow >= 0 else -1
        return {
            "size": size,
            "checked_out": checked_out,
            "overflow": overflow,
            "checked_in": checked_in,
            "max": configured_max,
        }
    except Exception:
        logger.exception("collect_resource_metrics: failed to read DB pool status")
        return {
            "size": -1,
            "checked_out": -1,
            "overflow": -1,
            "checked_in": -1,
            "max": -1,
            "error": "pool_status_unavailable",
        }


def _collect_publish_records(db: Any) -> dict[str, Any]:
    """活跃发布记录数 + 过期租约数（轻量 COUNT，需调用方传入 db）。

    两项都走索引列（status / lease_until），COUNT 廉价。失败返回占位、不抛错。
    """
    try:
        from sqlalchemy import func, select

        from server.app.core.time import utcnow
        from server.app.modules.tasks.models import PublishRecord

        active = (
            db.scalar(
                select(func.count())
                .select_from(PublishRecord)
                .where(PublishRecord.status == "running")
            )
            or 0
        )
        expired_lease = (
            db.scalar(
                select(func.count())
                .select_from(PublishRecord)
                .where(
                    PublishRecord.status == "running",
                    PublishRecord.lease_until.is_not(None),
                    PublishRecord.lease_until < utcnow(),
                )
            )
            or 0
        )
        return {"active_publish_records": int(active), "expired_leases": int(expired_lease)}
    except Exception:
        logger.exception("collect_resource_metrics: failed to count publish records")
        return {
            "active_publish_records": -1,
            "expired_leases": -1,
            "error": "publish_record_counts_unavailable",
        }


def collect_resource_metrics(db: Any | None = None) -> dict[str, Any]:
    """采集一份资源指标快照。

    - `db` 省略时：只返回池状态 + 闸占位 + run 计数占位（不开 session、零 IO 风险）。
    - 传入 `db`（一个 Session）时：附带活跃发布记录 / 过期租约的轻量 COUNT。

    任何子项失败都被吞掉为占位值，整份结果绝不抛错——后台采样线程可放心调用。
    """
    metrics: dict[str, Any] = {
        "pool": _collect_pool(),
        # 每个 ObservableGate 的 in_use/waiting/capacity（pipeline / scheme / publish）。
        "gates": _collect_gates(),
    }
    if db is not None:
        metrics.update(_collect_publish_records(db))
    else:
        # 未传 db：标明 run 指标未采集，而非伪造 0。
        metrics["active_publish_records"] = None
        metrics["expired_leases"] = None
    return metrics


# ── 启动期连接预算断言（Task 5，封堵 #4）────────────────────────────────────────
# Starlette/FastAPI 的同步端点经 AnyIO 默认线程池执行（默认 40 令牌），每个线程经 get_db 至多持
# 1 连接——故 anyio 大小是「同步端点」并发持连接数的上界。注意：pipeline / scheme 后台 run 走各自
# 的 ThreadPoolExecutor + 自建 session（**不占 anyio**），是 anyio 之外的额外借用方，但被各自闸
# 封顶（pipeline_max + scheme_max，默认 3 + 2）；scheme 侧 ×4 fan-out 实测为瞬时借还，pipeline 节点
# checkout 时长尚未做满负载实测（见计划 Task 5 Step 1）。这些零散/瞬时借用由 safety_margin 吸收、
# 不单列进式子——故本断言是保守护栏而非精确建模。我们刻意不缩 anyio：缩它会饿死 SSE / 同步端点
# （见 docs/plans/2026-06-16-resource-hardening.md Task 5、评审第 13 条）。
STARLETTE_DEFAULT_THREAD_POOL_SIZE = 40


def compute_connection_budget(
    *,
    anyio_pool_size: int,
    publish_max: int,
    safety_margin: int,
    pool_capacity: int,
) -> dict[str, Any]:
    """连接预算保守上界：`anyio + publish_max + safety_margin ≤ pool_capacity`。

    纯函数、可单测。返回算式明细 + `within_budget` 布尔（needed == capacity 算 within，边界不误报）。
    """
    needed = anyio_pool_size + publish_max + safety_margin
    return {
        "anyio_pool_size": anyio_pool_size,
        "publish_max": publish_max,
        "safety_margin": safety_margin,
        "needed": needed,
        "pool_capacity": pool_capacity,
        "within_budget": needed <= pool_capacity,
    }


def check_connection_budget() -> dict[str, Any]:
    """启动期连接预算检查：算式明细打日志；越界经 emit_resource_alert 统一告警（不只孤立 WARNING）。

    杠杆是扩池 / 降 publish_max，**绝不缩 anyio**。绝不抛错——预算检查失败不应阻塞 create_app。
    池容量不可用（max≤0，如 import 失败）时只记日志、不告警（容量未知 ≠ 越界，避免误报）。
    返回算式明细 dict（供测试与诊断）。
    """
    try:
        from server.app.core.config import get_settings

        settings = get_settings()
        pool = _collect_pool()
        raw_cap = pool.get("max", -1)
        pool_capacity = raw_cap if isinstance(raw_cap, int) else -1
        budget = compute_connection_budget(
            anyio_pool_size=STARLETTE_DEFAULT_THREAD_POOL_SIZE,
            publish_max=settings.publish_max_concurrent_records,
            safety_margin=settings.connection_budget_safety_margin,
            pool_capacity=pool_capacity,
        )
        logger.info(
            "connection budget: anyio=%d + publish_max=%d + margin=%d = need %d vs capacity %d (within=%s)",
            budget["anyio_pool_size"],
            budget["publish_max"],
            budget["safety_margin"],
            budget["needed"],
            budget["pool_capacity"],
            budget["within_budget"],
        )
        if pool_capacity > 0 and not budget["within_budget"]:
            emit_resource_alert(
                f"connection budget exceeded: need {budget['needed']} "
                f"(anyio {budget['anyio_pool_size']} + publish {budget['publish_max']} "
                f"+ margin {budget['safety_margin']}) > pool capacity {pool_capacity}. "
                "Raise GEO_DB_POOL_SIZE / GEO_DB_MAX_OVERFLOW or lower "
                "GEO_PUBLISH_MAX_CONCURRENT_RECORDS — do NOT shrink the anyio thread pool.",
                {"budget": budget},
            )
        return budget
    except Exception:
        logger.exception("check_connection_budget failed")
        return {}


def pool_occupancy_ratio(pool_metrics: dict[str, Any]) -> float | None:
    """checked_out / max 占用率；max 不可用（<=0）时返回 None。"""
    max_conn = pool_metrics.get("max")
    checked_out = pool_metrics.get("checked_out")
    if not isinstance(max_conn, int) or not isinstance(checked_out, int) or max_conn <= 0:
        return None
    return checked_out / max_conn


# ── 统一告警 hook ─────────────────────────────────────────────────────────────
# Task 5 将 import 本函数把"连接预算越界"等也接到同一通道。当前默认实现 = 升 WARNING。
# 可通过 set_alert_hook 注入自定义通道（如飞书），便于后续不改调用点扩展。
def _default_alert(message: str, context: dict[str, Any] | None = None) -> None:
    logger.warning("RESOURCE ALERT: %s | context=%s", message, context or {})


_alert_hook: Callable[[str, dict[str, Any] | None], None] = _default_alert


def emit_resource_alert(message: str, context: dict[str, Any] | None = None) -> None:
    """统一告警入口：Task 5 等可 import 调用，避免到处散落 logger.warning。

    永不抛错——告警通道故障不应拖垮采样/业务线程。
    """
    try:
        _alert_hook(message, context)
    except Exception:
        logger.exception("resource alert hook raised")


def set_alert_hook(hook: Callable[[str, dict[str, Any] | None], None]) -> None:
    """替换全局告警 hook（如接飞书）。传 None 行为未定义，调用方自负。"""
    global _alert_hook
    _alert_hook = hook


# ── 周期采样后台线程 ─────────────────────────────────────────────────────────
_sampler_thread: threading.Thread | None = None
_sampler_stop = threading.Event()


def sample_once(
    session_factory: SessionFactory | None,
    *,
    warn_ratio: float = 0.8,
) -> dict[str, Any]:
    """采集一份指标并打点到日志；占用率超阈值升 WARNING（走告警 hook）。

    纯函数式、可单测：传 None 不开 session（只采池/闸占位）。绝不抛错。
    """
    db = None
    try:
        if session_factory is not None:
            db = session_factory()
        metrics = collect_resource_metrics(db)
    except Exception:
        logger.exception("resource-metrics sample_once: collect failed")
        return {}
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                logger.exception("resource-metrics sample_once: db close failed")

    # 落盘 = 结构化日志行（轮转交给现有日志基础设施，不引入新 infra）。
    logger.info("resource-metrics sample: %s", metrics)
    ratio = pool_occupancy_ratio(metrics.get("pool", {}))
    if ratio is not None and ratio > warn_ratio:
        pool = metrics.get("pool", {})
        emit_resource_alert(
            f"DB pool occupancy {ratio:.0%} > {warn_ratio:.0%} threshold "
            f"(checked_out={pool.get('checked_out')}/{pool.get('max')})",
            {"pool": pool, "ratio": ratio},
        )
    return metrics


def start_resource_sampler(session_factory: SessionFactory) -> bool:
    """按配置启动后台采样守护线程。返回是否启动（关闭或已在运行返回 False）。

    线程 daemon=True、try/except 包住每轮采样，绝不因采样失败拖垮进程或阻塞启动。
    """
    global _sampler_thread
    from server.app.core.config import get_settings

    settings = get_settings()
    if not settings.resource_metrics_sampling_enabled:
        return False
    if _sampler_thread is not None and _sampler_thread.is_alive():
        return False

    _sampler_stop.clear()

    def _loop() -> None:
        while not _sampler_stop.is_set():
            interval = max(5, get_settings().resource_metrics_sample_interval_seconds)
            # 先等再采：避免一启动就采（启动期池本就空），停止事件可立即唤醒退出。
            if _sampler_stop.wait(interval):
                break
            try:
                sample_once(
                    session_factory,
                    warn_ratio=get_settings().resource_metrics_warn_ratio,
                )
            except Exception:
                logger.exception("resource-metrics sampler round failed")

    _sampler_thread = threading.Thread(target=_loop, daemon=True, name="resource-metrics-sampler")
    _sampler_thread.start()
    return True


def stop_resource_sampler() -> None:
    """请求停止采样线程（测试 / 优雅关闭用）。"""
    _sampler_stop.set()
