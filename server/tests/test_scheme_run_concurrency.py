"""scheme run 全局并发闸 `_RUN_GATE`（ObservableGate）契约（与 pipeline 的 executor._RUN_GATE 对称）。

run_scheme 用 `_RUN_GATE.acquire(timeout)` + finally release 包住 _run_scheme_inner，把单进程同时
执行的方案运行数限制在 GEO_SCHEME_MAX_CONCURRENT_RUNS（默认 2）。缺它时（改前 scheme run 完全无限流）
连点「运行方案」会无界 fork 后台线程、每个再开 4 个生文 worker 争抢 DB 连接，秒爆连接池（事故根因之一）。

照抄 test_pipeline_concurrency 的隔离手法：把闸换成小 cap、用栅栏阻住 inner、启动多于 cap 的
run，断言「并发进入数 ≤ cap」且「完成后槽位全部回收」。只验闸门本身，不起真生文。
"""

import threading
import time

import pytest

from server.app.shared.concurrency import ObservableGate
from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_run_scheme_honors_global_concurrency_cap_and_reclaims_slots(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation import scheme_executor

        cap = 2
        monkeypatch.setattr(scheme_executor, "_RUN_GATE", ObservableGate(cap))

        lock = threading.Lock()
        state = {"active": 0, "max_active": 0}
        entered = threading.Semaphore(0)  # 计数：有多少线程进了 inner
        release = threading.Event()  # 放行栅栏

        def fake_inner(run_id, session_factory):
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            entered.release()
            release.wait(timeout=5)
            with lock:
                state["active"] -= 1

        monkeypatch.setattr(scheme_executor, "_run_scheme_inner", fake_inner)

        n = cap + 2  # 启动比 cap 更多的 run
        threads = [
            threading.Thread(target=scheme_executor.run_scheme, args=(i, app.session_factory))
            for i in range(n)
        ]
        for t in threads:
            t.start()

        try:
            # 等到恰好 cap 个进入 inner；其余应被信号量挡在外面
            for _ in range(cap):
                assert entered.acquire(timeout=5), "应有 run 进入 inner（信号量未放行 = 死锁）"
            time.sleep(0.3)  # 给被挡住的线程机会去（若闸门失效则会错误地）越过信号量
            with lock:
                assert state["active"] == cap, f"并发进入数应恰为 cap={cap}，实际 {state['active']}"
                assert state["max_active"] <= cap
        finally:
            release.set()  # 无论断言成败都放行，避免线程悬挂
            for t in threads:
                t.join(timeout=5)

        with lock:
            assert state["max_active"] <= cap, "全程并发不得超过 cap"
            assert state["active"] == 0

        # 槽位已回收：全部完成后还能拿到 cap 个槽（finally release 正确执行的证据）
        gate = scheme_executor._RUN_GATE
        got = [gate.try_acquire() for _ in range(cap)]
        assert all(got), "完成后闸槽位应已全部释放"
    finally:
        app.cleanup()
