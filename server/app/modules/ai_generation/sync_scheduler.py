"""问题池定时镜像同步：应用内后台 daemon 线程。

设计要点：
- `run_sync_once(session_factory)` 是纯函数式的"扫描+同步一轮"，**不 sleep、可单测**——
  测试直接调它并 monkeypatch `list_bitable_records`，不跑真实 sleep、不打真实飞书。
- 后台线程只负责 `wait(interval) → run_sync_once` 循环；由 `create_app()` 在
  `GEO_QUESTION_POOL_AUTO_SYNC_ENABLED=true` 时启动。
- 每个池用独立 session + 独立事务，单池失败只记 `last_sync_error`，不影响其他池。
- 多进程部署注意：每个进程会各起一个同步线程；同步是幂等 upsert，重复无害但浪费，
  生产建议单进程跑 web，或后续加进程级租约。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from server.app.core.config import get_settings
from server.app.modules.ai_generation import question_bank as qb
from server.app.modules.ai_generation.models import QuestionPool

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Any]

_sync_thread: threading.Thread | None = None
_sync_stop = threading.Event()


def run_sync_once(session_factory: SessionFactory) -> dict[str, int]:
    """扫描"绑定飞书 + 未删除 + auto_sync_enabled"的池，逐池镜像同步一轮。

    返回 {"pools", "synced", "failed"}。单池失败 rollback 后单独事务写 last_sync_error。
    """
    db = session_factory()
    try:
        pool_ids = [
            p.id
            for p in db.query(QuestionPool)
            .filter(
                QuestionPool.is_deleted == False,  # noqa: E712
                QuestionPool.auto_sync_enabled == True,  # noqa: E712
                QuestionPool.feishu_app_token.is_not(None),
                QuestionPool.feishu_table_id.is_not(None),
            )
            .all()
        ]
    finally:
        db.close()

    synced = failed = 0
    for pid in pool_ids:
        db = session_factory()
        try:
            pool = db.get(QuestionPool, pid)
            if pool is None:
                continue
            qb.sync_pool(db, pool)
            db.commit()
            synced += 1
        except Exception as exc:  # noqa: BLE001 — 单池失败隔离，不影响其他池
            db.rollback()
            failed += 1
            try:
                pool = db.get(QuestionPool, pid)
                if pool is not None:
                    pool.last_sync_error = str(exc)[:1000]
                    db.commit()
            except Exception:
                db.rollback()
            logger.warning("auto-sync pool %s failed: %s", pid, exc)
        finally:
            db.close()

    return {"pools": len(pool_ids), "synced": synced, "failed": failed}


def start_auto_sync(session_factory: SessionFactory) -> bool:
    """按配置启动后台同步线程。返回是否启动（关闭或已在运行返回 False）。"""
    global _sync_thread
    settings = get_settings()
    if not settings.question_pool_auto_sync_enabled:
        return False
    if _sync_thread is not None and _sync_thread.is_alive():
        return False

    _sync_stop.clear()

    def _loop() -> None:
        while not _sync_stop.is_set():
            interval = max(60, get_settings().question_pool_sync_interval_seconds)
            # 先等再同步：避免一启动就打飞书；停止事件可立即唤醒退出
            if _sync_stop.wait(interval):
                break
            try:
                result = run_sync_once(session_factory)
                logger.info("question-pool auto-sync round: %s", result)
            except Exception:
                logger.exception("question-pool auto-sync round failed")

    _sync_thread = threading.Thread(target=_loop, daemon=True, name="question-pool-auto-sync")
    _sync_thread.start()
    return True


def stop_auto_sync() -> None:
    """请求停止后台线程（主要用于测试 / 优雅关闭）。"""
    _sync_stop.set()
