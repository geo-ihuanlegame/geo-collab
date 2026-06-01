"""
并发稳定性测试 — Phase 5

验证：
1. _global_publish_sem 在同一任务内正确限制并发（≤ MAX_CONCURRENT_RECORDS）
2. _global_publish_sem 跨任务共享（两个任务并发时，总浏览器进程 ≤ 5）
3. 发布抛异常时 semaphore 被正确释放（无泄漏）
4. 多任务并发执行时状态不互相污染
"""
import threading
import time
from io import BytesIO

import pytest

from server.app.modules.tasks.executor import MAX_CONCURRENT_RECORDS, _global_publish_sem
from server.tests.utils import build_test_app

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _create_article(client, title="Test Article") -> int:
    cover = client.post("/api/assets", files={"file": ("c.png", BytesIO(_PNG), "image/png")}).json()["id"]
    return client.post("/api/articles", json={
        "title": title, "content_json": {"type": "doc", "content": []},
        "plain_text": "body content", "cover_asset_id": cover,
    }).json()["id"]


def _create_account(test_app, key: str) -> int:
    d = test_app.data_dir / "browser_states" / "toutiao" / key
    d.mkdir(parents=True, exist_ok=True)
    (d / "storage_state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    return test_app.client.post(
        "/api/accounts/toutiao/login",
        json={"display_name": f"Acc {key}", "account_key": key, "use_browser": False},
    ).json()["id"]


def _create_single_task(client, article_id: int, account_id: int, name: str = "task") -> dict:
    return client.post("/api/tasks", json={
        "name": name,
        "task_type": "single",
        "article_id": article_id,
        "accounts": [{"account_id": account_id}],
    }).json()


class TestSemaphoreIntegrity:
    """验证 _global_publish_sem 的完整性（不依赖 Playwright）"""

    def test_semaphore_initial_value(self):
        """semaphore 初始 value 等于 MAX_CONCURRENT_RECORDS"""
        # 通过 acquire N 次确认可用槽数
        acquired = 0
        for _ in range(MAX_CONCURRENT_RECORDS):
            got = _global_publish_sem.acquire(blocking=False)
            if got:
                acquired += 1
        for _ in range(acquired):
            _global_publish_sem.release()
        assert acquired == MAX_CONCURRENT_RECORDS

    def test_semaphore_released_after_exception(self, monkeypatch):
        """发布过程抛异常时 semaphore 槽必须被释放"""
        from server.app.modules.tasks import executor as tasks_mod

        call_count = 0
        barrier = threading.Barrier(2)

        def raise_publisher(_record):
            nonlocal call_count
            call_count += 1

            def _runner(article, account, *, stop_before_publish=False):
                raise RuntimeError("simulated failure")

            return _runner

        monkeypatch.setattr(tasks_mod, "build_publish_runner_for_record", raise_publisher)

        test_app = build_test_app(monkeypatch)
        try:
            article_id = _create_article(test_app.client)
            account_id = _create_account(test_app, "acc-sem-exc")
            task_data = _create_single_task(test_app.client, article_id, account_id)
            task_id = task_data["id"]

            # 记录执行前 semaphore 状态（需要先把槽都 acquire 掉再 release 来测值）
            test_app.client.post(f"/api/tasks/{task_id}/execute")

            # 等待任务完成
            deadline = time.time() + 10.0
            while time.time() < deadline:
                records = test_app.client.get(f"/api/tasks/{task_id}/records").json()
                if records and records[0]["status"] in ("failed", "succeeded", "cancelled"):
                    break
                time.sleep(0.05)

            # 验证 semaphore 槽已被完整归还（可以 acquire MAX 次）
            acquired = 0
            for _ in range(MAX_CONCURRENT_RECORDS):
                if _global_publish_sem.acquire(blocking=False):
                    acquired += 1
            for _ in range(acquired):
                _global_publish_sem.release()
            assert acquired == MAX_CONCURRENT_RECORDS, (
                f"Semaphore leak: only {acquired}/{MAX_CONCURRENT_RECORDS} slots available after exception"
            )
        finally:
            test_app.cleanup()

    def test_semaphore_released_after_success(self, monkeypatch):
        """发布成功后 semaphore 槽被归还"""
        from server.app.modules.tasks import executor as tasks_mod

        class FakeResult:
            url = "https://toutiao.com/article/123"
            message = "Published"

        def ok_publisher(_record):
            def _runner(article, account, *, stop_before_publish=False):
                return FakeResult()
            return _runner

        monkeypatch.setattr(tasks_mod, "build_publish_runner_for_record", ok_publisher)

        test_app = build_test_app(monkeypatch)
        try:
            article_id = _create_article(test_app.client)
            account_id = _create_account(test_app, "acc-sem-ok")
            task_data = _create_single_task(test_app.client, article_id, account_id)
            task_id = task_data["id"]

            test_app.client.post(f"/api/tasks/{task_id}/execute")

            deadline = time.time() + 10.0
            while time.time() < deadline:
                records = test_app.client.get(f"/api/tasks/{task_id}/records").json()
                if records and records[0]["status"] == "succeeded":
                    break
                time.sleep(0.05)

            acquired = 0
            for _ in range(MAX_CONCURRENT_RECORDS):
                if _global_publish_sem.acquire(blocking=False):
                    acquired += 1
            for _ in range(acquired):
                _global_publish_sem.release()
            assert acquired == MAX_CONCURRENT_RECORDS, (
                f"Semaphore leak: only {acquired}/{MAX_CONCURRENT_RECORDS} slots after success"
            )
        finally:
            test_app.cleanup()


TERMINAL_STATUSES = {"succeeded", "partial_failed", "failed", "cancelled"}


def _wait_task_terminal(client, task_id: int, timeout: float = 15.0) -> dict:
    """Poll task until it reaches a terminal status."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        detail = client.get(f"/api/tasks/{task_id}").json()
        if detail.get("status") in TERMINAL_STATUSES:
            return detail
        time.sleep(0.05)
    return client.get(f"/api/tasks/{task_id}").json()


def test_same_account_across_tasks_is_serialized(monkeypatch):
    """Two tasks targeting the same account never run their publish runner concurrently."""
    from server.app.modules.tasks import executor as tasks_mod

    active = 0
    max_active = 0
    lock = threading.Lock()
    release_event = threading.Event()

    def counting_publisher(_record):
        def _runner(article, account, *, stop_before_publish=False):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            release_event.wait(timeout=2)
            with lock:
                active -= 1

            class R:
                url = None
                message = "ok"
            return R()
        return _runner

    monkeypatch.setattr(tasks_mod, "build_publish_runner_for_record", counting_publisher)

    test_app = build_test_app(monkeypatch)
    try:
        article1_id = _create_article(test_app.client, "Same Account One")
        article2_id = _create_article(test_app.client, "Same Account Two")
        account_id = _create_account(test_app, "same-account-cross-task")
        task1 = _create_single_task(test_app.client, article1_id, account_id, "same-account-1")
        task2 = _create_single_task(test_app.client, article2_id, account_id, "same-account-2")

        threading.Timer(0.3, release_event.set).start()
        threads = [
            threading.Thread(target=lambda tid=tid: test_app.client.post(f"/api/tasks/{tid}/execute"))
            for tid in (task1["id"], task2["id"])
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        _wait_task_terminal(test_app.client, task1["id"])
        _wait_task_terminal(test_app.client, task2["id"])

        assert max_active == 1
    finally:
        test_app.cleanup()


class TestConcurrentTaskIsolation:
    """验证多任务并发执行时数据隔离"""

    def test_two_tasks_records_independent(self, monkeypatch):
        """两个任务并发执行，每个任务的 records 状态独立，互不污染"""
        from server.app.modules.tasks import executor as tasks_mod

        class FakeResult:
            url = "https://toutiao.com/article/ok"
            message = "Published"

        def ok_publisher(_record):
            def _runner(article, account, *, stop_before_publish=False):
                time.sleep(0.05)
                return FakeResult()
            return _runner

        monkeypatch.setattr(tasks_mod, "build_publish_runner_for_record", ok_publisher)

        test_app = build_test_app(monkeypatch)
        try:
            article1_id = _create_article(test_app.client, "Article One")
            article2_id = _create_article(test_app.client, "Article Two")
            account1_id = _create_account(test_app, "acc-iso-1")
            account2_id = _create_account(test_app, "acc-iso-2")

            task1 = _create_single_task(test_app.client, article1_id, account1_id, "Task One")
            task2 = _create_single_task(test_app.client, article2_id, account2_id, "Task Two")

            # execute 端点立即返回，任务在后台线程执行；并发触发两个任务
            errors: list[str] = []

            def run_task(task_id: int):
                try:
                    test_app.client.post(f"/api/tasks/{task_id}/execute")
                except Exception as e:
                    errors.append(f"task {task_id}: {e}")

            t1 = threading.Thread(target=run_task, args=(task1["id"],))
            t2 = threading.Thread(target=run_task, args=(task2["id"],))
            t1.start(); t2.start()
            t1.join(timeout=10); t2.join(timeout=10)
            assert not errors, f"Concurrent task errors: {errors}"

            # 等待两个任务都到达终态
            task1_detail = _wait_task_terminal(test_app.client, task1["id"])
            task2_detail = _wait_task_terminal(test_app.client, task2["id"])

            assert task1_detail["status"] == "succeeded", f"Task1: {task1_detail['status']}"
            assert task2_detail["status"] == "succeeded", f"Task2: {task2_detail['status']}"

            # 每个任务的 record 独立成功
            records1 = test_app.client.get(f"/api/tasks/{task1['id']}/records").json()
            records2 = test_app.client.get(f"/api/tasks/{task2['id']}/records").json()
            assert records1[0]["status"] == "succeeded"
            assert records2[0]["status"] == "succeeded"
        finally:
            test_app.cleanup()

    def test_semaphore_blocks_excess_concurrent_records(self, monkeypatch):
        """semaphore 跨任务限制：N+2 个任务并发，观察到最大并发 ≤ MAX_CONCURRENT_RECORDS"""
        from server.app.modules.tasks import executor as tasks_mod

        max_concurrent = [0]
        current_concurrent = [0]
        count_lock = threading.Lock()
        # 所有 publisher 线程一起释放，制造并发压力
        release_event = threading.Event()

        def counting_publisher(_record):
            def _runner(article, account, *, stop_before_publish=False):
                with count_lock:
                    current_concurrent[0] += 1
                    if current_concurrent[0] > max_concurrent[0]:
                        max_concurrent[0] = current_concurrent[0]
                release_event.wait(timeout=3)
                with count_lock:
                    current_concurrent[0] -= 1

                class R:
                    url = None
                    message = "ok"
                return R()
            return _runner

        monkeypatch.setattr(tasks_mod, "build_publish_runner_for_record", counting_publisher)

        test_app = build_test_app(monkeypatch)
        try:
            # 创建 MAX+2 个独立的 single 任务（每个任务1个账号）
            n = MAX_CONCURRENT_RECORDS + 2
            task_ids = []
            for i in range(n):
                article_id = _create_article(test_app.client, f"Stress Article {i}")
                account_id = _create_account(test_app, f"acc-str-{i}")
                t = _create_single_task(test_app.client, article_id, account_id, f"stress-{i}")
                task_ids.append(t["id"])

            # 短暂等待后释放所有 publisher（让它们在 semaphore 内同时运行）
            threading.Timer(0.3, release_event.set).start()

            # 并发触发所有任务
            exec_errors: list[str] = []

            def exec_task(tid: int):
                try:
                    test_app.client.post(f"/api/tasks/{tid}/execute")
                except Exception as e:
                    exec_errors.append(str(e))

            threads = [threading.Thread(target=exec_task, args=(tid,)) for tid in task_ids]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=15)

            # 等待所有任务完成
            for tid in task_ids:
                _wait_task_terminal(test_app.client, tid, timeout=10)

            assert not exec_errors, f"Execute errors: {exec_errors}"
            assert max_concurrent[0] <= MAX_CONCURRENT_RECORDS, (
                f"Semaphore violated: {max_concurrent[0]} concurrent > {MAX_CONCURRENT_RECORDS} limit"
            )
        finally:
            test_app.cleanup()
