"""Task 7（封堵 #6）：web 进程发布只入队、不起浏览器 + 无 worker 告警 —— 行为契约。

根因：`_global_publish_gate` 是进程内信号量；若 web 进程也内联跑发布，则 web + 单实例 worker
双进程各自 ×N，封顶失效。修法：

1. **web 只入队**：`POST /api/tasks/{id}/execute` 在生产（未显式开内联）只把任务/记录留 pending +
   释放陈旧 worker 认领，绝不在 web 进程里调 `execute_task` 起浏览器发布。由单实例 worker 抢占。
2. **显式开关**：测试 / 单机 dev 经 `inline_execute_enabled=True`（+ bg_session_factory）才内联执行。
3. **无 worker 告警**：入队时若无新鲜 WorkerHeartbeat（30s 内心跳），走 emit_resource_alert 告警 +
   回包 `worker_online=False`，避免任务静默卡 pending。
"""

from __future__ import annotations

from datetime import timedelta
from io import BytesIO

import pytest

from server.tests.utils import build_test_app

# 注意：不要在模块顶层 import server.app.modules.tasks.router —— 它会在 collection 期触发
# server.app.db.session 的模块级 get_database_url()（饿汉建引擎），而 collection 早于
# build_test_app 注入 GEO_DATABASE_URL/GEO_DATA_DIR，CI 无 .env 时直接 RuntimeError 收集失败。
# 改为各测试内、build_test_app 之后再 lazy import（与仓库内其它 DB 测试一致）。

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _create_article(client) -> int:
    cover = client.post(
        "/api/assets", files={"file": ("c.png", BytesIO(_PNG), "image/png")}
    ).json()["id"]
    return client.post(
        "/api/articles",
        json={
            "title": "Web Enqueue",
            "content_json": {"type": "doc", "content": []},
            "plain_text": "body content",
            "cover_asset_id": cover,
        },
    ).json()["id"]


def _create_account(test_app, key: str) -> int:
    d = test_app.data_dir / "browser_states" / "toutiao" / key
    d.mkdir(parents=True, exist_ok=True)
    (d / "storage_state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    return test_app.client.post(
        "/api/accounts/toutiao/login",
        json={"display_name": f"Acc {key}", "account_key": key, "use_browser": False},
    ).json()["id"]


def _create_single_task(client, article_id: int, account_id: int) -> int:
    return client.post(
        "/api/tasks",
        json={
            "name": "web-enqueue-task",
            "task_type": "single",
            "article_id": article_id,
            "accounts": [{"account_id": account_id}],
        },
    ).json()["id"]


def _insert_fresh_worker(test_app) -> None:
    from server.app.core.time import utcnow
    from server.app.modules.system.models import WorkerHeartbeat

    with test_app.session_factory() as db:
        db.merge(WorkerHeartbeat(worker_id="w-test", hostname="h", pid=1, heartbeat_at=utcnow()))
        db.commit()


@pytest.mark.mysql
def test_web_execute_enqueues_without_publishing(monkeypatch):
    """生产路径（内联关）：execute 只入队，绝不在 web 进程调 execute_task；记录留 pending。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.tasks import router as tasks_router

        # 模拟生产：关掉内联开关（建任务时的自动续跑也随之 no-op，不会偷跑发布）
        monkeypatch.setattr(tasks_router, "inline_execute_enabled", False)

        calls: list = []
        monkeypatch.setattr(tasks_router, "execute_task", lambda *a, **k: calls.append(1))

        client = test_app.client
        article_id = _create_article(client)
        account_id = _create_account(test_app, "web-enq")
        task_id = _create_single_task(client, article_id, account_id)

        r = client.post(f"/api/tasks/{task_id}/execute")
        assert r.status_code == 202, r.text

        # web 进程绝不调 execute_task（生产路径同步、无线程；调用计数必为 0）
        assert calls == [], "web 进程不应在本进程内发布（execute_task 被调用了）"

        # 记录仍 pending，等 worker 抢占
        from server.app.modules.tasks.service import list_task_records

        with test_app.session_factory() as db:
            records = list_task_records(db, task_id)
        assert records and all(r.status == "pending" for r in records)
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_web_execute_alerts_when_no_fresh_worker(monkeypatch):
    """入队时无新鲜 worker：走告警 hook + 回包 worker_online=False（不静默卡 pending）。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.tasks import router as tasks_router
        from server.app.shared import resource_metrics as rm

        monkeypatch.setattr(tasks_router, "inline_execute_enabled", False)
        monkeypatch.setattr(tasks_router, "execute_task", lambda *a, **k: None)

        alerts: list = []
        monkeypatch.setattr(rm, "_alert_hook", lambda msg, ctx=None: alerts.append((msg, ctx)))

        client = test_app.client
        article_id = _create_article(client)
        account_id = _create_account(test_app, "web-noworker")
        task_id = _create_single_task(client, article_id, account_id)

        r = client.post(f"/api/tasks/{task_id}/execute")
        assert r.status_code == 202, r.text
        assert r.json().get("worker_online") is False
        assert len(alerts) == 1
        assert "worker" in alerts[0][0].lower()
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_web_execute_no_alert_when_worker_fresh(monkeypatch):
    """入队时有新鲜 worker：不告警，回包 worker_online=True。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.tasks import router as tasks_router
        from server.app.shared import resource_metrics as rm

        monkeypatch.setattr(tasks_router, "inline_execute_enabled", False)
        monkeypatch.setattr(tasks_router, "execute_task", lambda *a, **k: None)

        alerts: list = []
        monkeypatch.setattr(rm, "_alert_hook", lambda msg, ctx=None: alerts.append((msg, ctx)))

        client = test_app.client
        article_id = _create_article(client)
        account_id = _create_account(test_app, "web-freshworker")
        task_id = _create_single_task(client, article_id, account_id)
        _insert_fresh_worker(test_app)

        r = client.post(f"/api/tasks/{task_id}/execute")
        assert r.status_code == 202, r.text
        assert r.json().get("worker_online") is True
        assert alerts == []
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_stale_worker_heartbeat_not_considered_fresh(monkeypatch):
    """陈旧心跳（>30s）不算新鲜 worker：仍告警。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.core.time import utcnow
        from server.app.modules.system.models import WorkerHeartbeat
        from server.app.modules.tasks import router as tasks_router
        from server.app.shared import resource_metrics as rm

        monkeypatch.setattr(tasks_router, "inline_execute_enabled", False)
        monkeypatch.setattr(tasks_router, "execute_task", lambda *a, **k: None)
        alerts: list = []
        monkeypatch.setattr(rm, "_alert_hook", lambda msg, ctx=None: alerts.append((msg, ctx)))

        with test_app.session_factory() as db:
            db.merge(
                WorkerHeartbeat(
                    worker_id="w-stale",
                    hostname="h",
                    pid=1,
                    heartbeat_at=utcnow() - timedelta(seconds=120),
                )
            )
            db.commit()

        client = test_app.client
        article_id = _create_article(client)
        account_id = _create_account(test_app, "web-staleworker")
        task_id = _create_single_task(client, article_id, account_id)

        r = client.post(f"/api/tasks/{task_id}/execute")
        assert r.status_code == 202, r.text
        assert r.json().get("worker_online") is False
        assert len(alerts) == 1
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_explicit_switch_runs_inline(monkeypatch):
    """显式开关（build_test_app 默认 inline_execute_enabled=True）：execute 仍内联执行。"""
    import threading

    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.tasks import router as tasks_router

        ran = threading.Event()
        monkeypatch.setattr(tasks_router, "execute_task", lambda *a, **k: ran.set())

        client = test_app.client
        article_id = _create_article(client)
        account_id = _create_account(test_app, "web-inline")
        task_id = _create_single_task(client, article_id, account_id)

        r = client.post(f"/api/tasks/{task_id}/execute")
        assert r.status_code == 202, r.text
        assert ran.wait(timeout=5), "显式开关开启时应内联执行 execute_task"
    finally:
        test_app.cleanup()
