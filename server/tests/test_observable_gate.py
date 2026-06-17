"""Task 4 —— ObservableGate（可观测、带超时的并发闸）纯逻辑单测。

替换三处裸 threading.Semaphore（占用读不出、acquire 无超时）。本测覆盖闸自身契约：
容量限制、try_acquire 非阻塞、acquire 超时返回 False、in_use/waiting 计数准确、
release 后可再取、over-release 抛错（守护"释放漏口/多放"这类 bug，见评审第 14 条）。
纯逻辑、无 DB。
"""

from __future__ import annotations

import threading
import time

from server.app.shared.concurrency import ObservableGate


def _wait_until(predicate, *, deadline_seconds: float = 2.0) -> bool:
    end = time.monotonic() + deadline_seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_try_acquire_respects_capacity():
    gate = ObservableGate(2, name="t")
    assert gate.try_acquire() is True
    assert gate.try_acquire() is True
    assert gate.try_acquire() is False  # 满
    assert gate.in_use == 2

    gate.release()
    assert gate.in_use == 1
    assert gate.try_acquire() is True  # 释放后腾出一个槽
    assert gate.in_use == 2


def test_acquire_timeout_returns_false_when_full():
    gate = ObservableGate(1)
    assert gate.acquire(timeout=0.5) is True  # 拿到唯一槽

    start = time.monotonic()
    got = gate.acquire(timeout=0.05)  # 满 → 超时
    elapsed = time.monotonic() - start

    assert got is False
    assert elapsed >= 0.05
    assert gate.in_use == 1  # 超时不增计数


def test_release_after_acquire_allows_reacquire():
    gate = ObservableGate(1)
    assert gate.acquire(timeout=0.5) is True
    gate.release()
    assert gate.in_use == 0
    assert gate.acquire(timeout=0.5) is True


def test_waiting_reflects_blocked_threads():
    gate = ObservableGate(1)
    assert gate.try_acquire() is True  # 占满

    blocked_result: dict[str, bool] = {}

    def _blocker() -> None:
        blocked_result["got"] = gate.acquire(timeout=2.0)

    t = threading.Thread(target=_blocker)
    t.start()
    try:
        # 该线程必阻塞在 acquire 上 → waiting 升到 1
        assert _wait_until(lambda: gate.waiting == 1), f"waiting stuck at {gate.waiting}"
        gate.release()  # 让出槽，阻塞线程应拿到
        t.join(timeout=2.0)
        assert blocked_result.get("got") is True
        assert gate.waiting == 0
    finally:
        if t.is_alive():
            t.join(timeout=1.0)


def test_over_release_raises():
    gate = ObservableGate(1)
    # 没持有就 release：BoundedSemaphore 守护，抛 ValueError（暴露"多放/释放漏口"bug）
    try:
        gate.release()
        raised = False
    except ValueError:
        raised = True
    assert raised is True
    assert gate.in_use == 0  # 抛错不得污染计数
