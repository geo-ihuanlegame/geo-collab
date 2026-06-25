"""统一日志配置 + 运行上下文关联（Layer 0 + 1）。

为什么需要：项目此前没有任何 logging 配置，根 logger 默认 WARNING，各模块的 logger.info(...)
基本看不到。本模块在所有进程入口（web create_app / 发布 worker / MCP）调一次 configure_logging()，
把根 logger 级别设为 GEO_LOG_LEVEL（默认 INFO）、统一格式、同时输出 stdout + 滚动文件。

运行上下文关联：用 contextvars 存当前 pipeline run 的 run_id / pipeline_id / node 标识，
RunContextFilter 把它们注入每条日志行（如 `[run=123 pipe=1 node=2:ai_compose]`）。
这样执行器、节点内部、article_writer、litellm 包装层的日志都自动带上下文，grep 一个 run_id
即可串起整条链路，无需在每个 log 调用里手传。

⚠️ contextvars 不跨线程自动继承：节点内部用 ThreadPoolExecutor 并发生文时，子线程拿不到
父线程绑定的上下文。用 submit_in_context() 提交即可把当前上下文带进子线程。
"""

from __future__ import annotations

import contextvars
import logging
import logging.handlers
import sys
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

# ── 运行上下文（每个 pipeline run / 节点执行期间绑定）─────────────────────────
_run_id: contextvars.ContextVar[int | None] = contextvars.ContextVar("geo_run_id", default=None)
_pipeline_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "geo_pipeline_id", default=None
)
_node_label: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "geo_node_label", default=None
)

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(runctx)s%(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_configured = False


def bind_run(run_id: int | None, pipeline_id: int | None = None) -> None:
    """绑定当前线程的运行上下文（run 开始时调）。"""
    _run_id.set(run_id)
    _pipeline_id.set(pipeline_id)
    _node_label.set(None)


def bind_node(node_index: int | None, node_type: str | None = None) -> None:
    """绑定当前正在执行的节点标识（每个节点开始时调）。"""
    if node_index is None:
        _node_label.set(None)
    else:
        _node_label.set(f"{node_index}:{node_type}" if node_type else str(node_index))


def clear_run_context() -> None:
    """清空运行上下文（run 结束时调，避免污染后续复用线程的日志）。"""
    _run_id.set(None)
    _pipeline_id.set(None)
    _node_label.set(None)


class RunContextFilter(logging.Filter):
    """把 contextvars 里的运行上下文拼成 record.runctx，供格式串 %(runctx)s 使用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        parts: list[str] = []
        rid = _run_id.get()
        pid = _pipeline_id.get()
        node = _node_label.get()
        if rid is not None:
            parts.append(f"run={rid}")
        if pid is not None:
            parts.append(f"pipe={pid}")
        if node is not None:
            parts.append(f"node={node}")
        record.runctx = f"[{' '.join(parts)}] " if parts else ""
        return True


def submit_in_context(executor: ThreadPoolExecutor, fn: Callable[..., Any], *args: Any) -> Future:
    """在「捕获当前运行上下文」的前提下把 fn 提交到线程池。

    contextvars 不跨线程继承，直接 pool.submit 会让子线程丢失 run/node 上下文。本函数
    用 copy_context() 复制当前上下文、在子线程内 ctx.run(fn) 执行，使生文等子任务的日志
    仍带正确的 [run=.. node=..]。
    """
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, fn, *args)


def configure_logging() -> None:
    """配置根 logger：级别 + stdout handler + （可选）滚动文件 handler。幂等，可多次调用。

    级别 / 是否落文件 / 保留天数读 Settings（GEO_LOG_LEVEL / GEO_LOG_TO_FILE / GEO_LOG_FILE_BACKUP_DAYS）。
    不动 uvicorn 自己的 logger；只在根 logger 上加 handler，应用各模块 logger 默认 propagate 到根。
    """
    global _configured
    if _configured:
        return

    # 延迟导入避免与 config 的循环依赖（config 不依赖本模块）。
    from server.app.core.config import get_settings

    settings = get_settings()
    level = getattr(logging, (settings.log_level or "INFO").upper(), logging.INFO)
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
    ctx_filter = RunContextFilter()

    root = logging.getLogger()
    root.setLevel(level)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    stream.addFilter(ctx_filter)
    root.addHandler(stream)

    if settings.log_to_file:
        try:
            from server.app.core.paths import get_data_dir

            logs_dir = get_data_dir() / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.TimedRotatingFileHandler(
                logs_dir / "app.log",
                when="midnight",
                backupCount=max(0, settings.log_file_backup_days),
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(ctx_filter)
            root.addHandler(file_handler)
        except Exception:  # noqa: BLE001 — 文件日志初始化失败不应阻断启动
            root.warning("文件日志 handler 初始化失败，仅输出 stdout", exc_info=True)

    _configured = True
    root.info("logging configured: level=%s to_file=%s", settings.log_level, settings.log_to_file)
