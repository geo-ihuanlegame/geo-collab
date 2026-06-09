"""发布诊断事件采集（按线程隔离）。

发布运行器在 capture_publish_diagnostics(...) 作用域内执行，期间各处调用
record_publish_diagnostic / publish_step 把事件追加进当前线程绑定的列表。
用 threading.local 隔离，所以并发跑的多条发布记录互不串台。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PublishDiagnosticEvent:
    level: str
    message: str
    screenshot: bytes | None = None


_local = threading.local()


def _current_events() -> list[PublishDiagnosticEvent] | None:
    events = getattr(_local, "events", None)
    return events if isinstance(events, list) else None


@contextmanager
def capture_publish_diagnostics(events: list[PublishDiagnosticEvent]) -> Iterator[None]:
    """把传入列表绑定为当前线程的事件收集器，退出时恢复上一层（支持嵌套）。"""
    previous = getattr(_local, "events", None)
    _local.events = events
    try:
        yield
    finally:
        if previous is None:
            try:
                delattr(_local, "events")
            except AttributeError:
                pass
        else:
            _local.events = previous


def record_publish_diagnostic(
    message: str, *, level: str = "info", screenshot: bytes | None = None
) -> None:
    events = _current_events()
    if events is not None:
        events.append(PublishDiagnosticEvent(level=level, message=message, screenshot=screenshot))


def _safe_screenshot(page: Any | None) -> bytes | None:
    if page is None:
        return None
    try:
        return page.screenshot(full_page=True)
    except Exception:
        return None


@contextmanager
def publish_step(name: str, *, page: Any | None = None) -> Iterator[None]:
    """包裹一个发布步骤：记录耗时；失败时记录 error 级别事件并附截图后重新抛出。"""
    started = time.monotonic()
    try:
        yield
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        record_publish_diagnostic(
            f"step failed: {name}; elapsed_ms={elapsed_ms}; error={type(exc).__name__}: {exc}",
            level="error",
            screenshot=_safe_screenshot(page),
        )
        raise
    else:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        record_publish_diagnostic(f"step completed: {name}; elapsed_ms={elapsed_ms}")
