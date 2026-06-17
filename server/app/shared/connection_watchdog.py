"""Task G —— 运行期长持连接护栏（防 A 类复发：慢 IO 期间持 DB 连接，#1/#110 同源）。

SQLAlchemy `checkout` 事件记下借出时刻 + 线程名（轻量、无 IO）；`checkin` 时若持有时长超阈值，
经统一告警 hook（`resource_metrics.emit_resource_alert`）升 WARNING，并带上线程名作调用点线索
（配合 Task 3 给发布/生文线程池加的 `thread_name_prefix`，可定位是哪条路径长持）。

纪律靠文档 + 一次性 grep 拦不住（#110/#1 同源已两发），故用运行期断言自动捕获任何路径新引入
的长持。开关 + 阈值走环境变量，与 `session.py` 里池参数（`GEO_DB_POOL_SIZE` 等）同一处理方式
（引擎级旋钮在 import 期定，直读 `os.environ`、不进 Settings 缓存）：
- `GEO_CONNECTION_WATCHDOG_ENABLED`：默认 true，置 "false"/"0"/"off" 关闭。
- `GEO_CONNECTION_WATCHDOG_THRESHOLD_SECONDS`：默认 30。

开销：仅在 checkin 时计算——正常毫秒级短借只多一次字典存取 + 一次减法比较，无显著开销。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from server.app.shared.resource_metrics import emit_resource_alert

logger = logging.getLogger(__name__)

_INFO_KEY = "_connection_watchdog_checkout"

Clock = Callable[[], float]
Alert = Callable[[str, dict[str, Any] | None], None]


class ConnectionWatchdog:
    """checkout / checkin 事件处理器：连接持有超阈值即告警。

    `clock` / `alert` 可注入，便于纯逻辑单测（用假时钟推进时间、用 spy 捕获告警）。
    """

    def __init__(
        self,
        threshold_seconds: float,
        *,
        clock: Clock = time.monotonic,
        alert: Alert = emit_resource_alert,
    ) -> None:
        self._threshold = float(threshold_seconds)
        self._clock = clock
        self._alert = alert

    def on_checkout(self, _dbapi_conn: Any, connection_record: Any, _connection_proxy: Any) -> None:
        """SQLAlchemy `checkout` 事件签名 (dbapi_connection, connection_record, connection_proxy)。"""
        # 轻量上下文：借出时刻 + 线程名。info 跨 checkout/checkin 持久（同一物理连接）。
        try:
            connection_record.info[_INFO_KEY] = (
                self._clock(),
                threading.current_thread().name,
            )
        except Exception:  # 护栏绝不拖垮借连接
            logger.exception("connection watchdog on_checkout failed")

    def on_checkin(self, _dbapi_conn: Any, connection_record: Any) -> None:
        """SQLAlchemy `checkin` 事件签名 (dbapi_connection, connection_record)。"""
        try:
            data = connection_record.info.pop(_INFO_KEY, None)
            if not data:
                return  # 注册前就借出的连接归还：无记录，安全跳过、不误报
            checkout_at, thread_name = data
            held = self._clock() - checkout_at
            if held >= self._threshold:
                self._alert(
                    f"DB connection held {held:.1f}s >= {self._threshold:.0f}s threshold "
                    f"(thread={thread_name}) —— 慢 IO 期间疑似持连接，查该线程的调用路径",
                    {
                        "held_seconds": held,
                        "thread": thread_name,
                        "threshold": self._threshold,
                    },
                )
        except Exception:
            logger.exception("connection watchdog on_checkin failed")


_registered_engines: set[int] = set()


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def register_connection_watchdog(engine: Any) -> bool:
    """按环境变量在 `engine` 上注册 checkout/checkin 监听。返回是否注册。

    幂等：同一 engine 多次调用只注册一次（测试反复建 app 时不叠加监听）。关闭时直接返回 False。
    """
    from sqlalchemy import event

    if not _env_flag("GEO_CONNECTION_WATCHDOG_ENABLED", True):
        return False
    if id(engine) in _registered_engines:
        return False

    threshold = _env_float("GEO_CONNECTION_WATCHDOG_THRESHOLD_SECONDS", 30.0)
    watchdog = ConnectionWatchdog(threshold)
    event.listen(engine, "checkout", watchdog.on_checkout)
    event.listen(engine, "checkin", watchdog.on_checkin)
    _registered_engines.add(id(engine))
    logger.info("connection watchdog registered (threshold=%.0fs)", threshold)
    return True
