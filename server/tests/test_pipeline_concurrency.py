"""pipeline 全局并发闸 `_RUN_SEMAPHORE` 契约（此前零测试）。

run_pipeline 用 `with _RUN_SEMAPHORE:` 包住 _run_pipeline_inner，把单进程同时执行的 pipeline run
数限制在 GEO_PIPELINE_MAX_CONCURRENT_RUNS（默认 3）。本测试隔离这道闸本身：把信号量换成小 cap、
用 barrier 阻住 inner，启动多于 cap 的 run，断言「并发进入数 ≤ cap」且「完成后槽位全部回收」。
真实节点的并发执行由其它单线程用例覆盖，这里只验闸门，避免起多条真 pipeline 带来的不确定性。

为何重要：闸门若失效（比如重构成手动 acquire/release 漏了 release、或没包住 inner），会超并发跑
run → 打满 DB 连接 / LLM 限流，且不会有任何现有测试报警（现有 Semaphore 测试测的是 tasks 的
_global_publish_sem，与此无关）。
"""

import threading
import time

import pytest

from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_run_pipeline_honors_global_concurrency_cap_and_reclaims_slots(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines import executor

        cap = 2
        monkeypatch.setattr(executor, "_RUN_SEMAPHORE", threading.Semaphore(cap))

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

        monkeypatch.setattr(executor, "_run_pipeline_inner", fake_inner)

        n = cap + 2  # 启动比 cap 更多的 run
        threads = [
            threading.Thread(target=executor.run_pipeline, args=(i, app.session_factory))
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

        # 槽位已回收：全部完成后还能拿到 cap 个槽（with 语句正确 release 的证据）
        sem = executor._RUN_SEMAPHORE
        got = [sem.acquire(blocking=False) for _ in range(cap)]
        assert all(got), "完成后信号量槽位应已全部释放"
    finally:
        app.cleanup()
