"""Task 4 —— 可观测、带超时的并发闸（替换三处裸 threading.Semaphore）。

裸 `threading.Semaphore` 的三个毛病：占用读不出（无法上报 in_use/waiting）、`acquire` 无超时
（满了无限阻塞，慢 run 占槽 ~25min，见 #9）、不计 run 内 ×4 fan-out。`ObservableGate` 用
`BoundedSemaphore` + 受锁保护的计数器解决前两条（fan-out 预算由调用方/Task 5 断言治理）。

- `acquire(timeout)`：阻塞至多 timeout 秒，拿到返回 True、超时返回 False（绝不无限阻塞）。
- `try_acquire()`：非阻塞，立即拿到返回 True、否则 False（publish 主线程 submit 前用，见 #8）。
- `release()`：归还一个槽；over-release 由 BoundedSemaphore 抛 ValueError（暴露释放漏口 bug）。
- `in_use` / `waiting`：当前占用槽数 / 当前阻塞在 acquire 的线程数，供 resource_metrics 上报。
"""

from __future__ import annotations

import threading


class ObservableGate:
    def __init__(self, capacity: int, *, name: str = "") -> None:
        if capacity < 1:
            raise ValueError("ObservableGate capacity must be >= 1")
        self._sem = threading.BoundedSemaphore(capacity)
        self._capacity = capacity
        self._name = name
        self._lock = threading.Lock()
        self._in_use = 0
        self._waiting = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def in_use(self) -> int:
        with self._lock:
            return self._in_use

    @property
    def waiting(self) -> int:
        with self._lock:
            return self._waiting

    def try_acquire(self) -> bool:
        """非阻塞获取一个槽。拿到 True、满了 False。"""
        acquired = self._sem.acquire(blocking=False)
        if acquired:
            with self._lock:
                self._in_use += 1
        return acquired

    def acquire(self, timeout: float | None = None) -> bool:
        """阻塞至多 timeout 秒获取一个槽。拿到 True、超时 False。timeout=None 为无限等待。"""
        with self._lock:
            self._waiting += 1
        try:
            if timeout is None:
                acquired = self._sem.acquire()
            else:
                acquired = self._sem.acquire(timeout=timeout)
        finally:
            with self._lock:
                self._waiting -= 1
        if acquired:
            with self._lock:
                self._in_use += 1
        return acquired

    def release(self) -> None:
        """归还一个槽。先 release（over-release 在此抛 ValueError、不污染计数），再减计数。"""
        self._sem.release()
        with self._lock:
            self._in_use -= 1

    def snapshot(self) -> dict[str, int | str]:
        """一份占用快照，供 resource_metrics 汇总（in_use/waiting/capacity）。"""
        with self._lock:
            return {
                "name": self._name,
                "capacity": self._capacity,
                "in_use": self._in_use,
                "waiting": self._waiting,
            }
