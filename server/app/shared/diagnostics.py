from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator


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


def record_publish_diagnostic(message: str, *, level: str = "info", screenshot: bytes | None = None) -> None:
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
