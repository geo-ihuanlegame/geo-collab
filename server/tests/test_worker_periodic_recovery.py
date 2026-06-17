"""Task 2 / #7：worker 主循环周期复位卡死记录 + 过期认领。

`recover_stuck_records` / `recover_stuck_task_claims` 此前只在 `_startup` 跑一次；
进程长期运行时，崩溃后靠 lease 过期恢复的卡死记录不会自愈、永久占着账号/profile 锁。
本测试驱动一轮「周期恢复」分支，断言：
  - 过期 lease 的 running 记录被拨回 pending；
  - 过期 lease 的 worker 认领被清空（worker_id 归零）；
  - lease 未过期的在跑记录/认领绝不被误伤（判别性对照）。
"""

from datetime import timedelta
from io import BytesIO

from server.app.core.time import utcnow
from server.app.modules.tasks.models import PublishRecord, PublishTask
from server.tests.utils import build_test_app

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _create_publishable_task(test_app) -> int:
    client = test_app.client
    cover = client.post(
        "/api/assets", files={"file": ("cover.png", BytesIO(_PNG), "image/png")}
    ).json()["id"]
    article = client.post(
        "/api/articles",
        json={
            "title": "Periodic Recovery Article",
            "content_json": {"type": "doc", "content": []},
            "plain_text": "body",
            "cover_asset_id": cover,
        },
    ).json()
    state_dir = test_app.data_dir / "browser_states" / "toutiao" / "periodic-recovery"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "storage_state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    account = client.post(
        "/api/accounts/toutiao/login",
        json={
            "display_name": "Periodic Recovery",
            "account_key": "periodic-recovery",
            "use_browser": False,
        },
    ).json()
    task = client.post(
        "/api/tasks",
        json={
            "name": "periodic recovery",
            "task_type": "single",
            "article_id": article["id"],
            "accounts": [{"account_id": account["id"]}],
        },
    ).json()
    return task["id"]


def test_periodic_recovery_resets_expired_record_and_claim(monkeypatch):
    """一轮周期恢复后：过期 lease 的 running 记录拨回 pending、过期 worker 认领被清空。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.worker import executor

        task_id = _create_publishable_task(test_app)
        with test_app.session_factory() as db:
            task = db.get(PublishTask, task_id)
            task.worker_id = "dead-worker-periodic"
            task.worker_lease_until = utcnow() - timedelta(minutes=5)
            task.worker_heartbeat_at = utcnow() - timedelta(minutes=5)
            rec = db.query(PublishRecord).filter(PublishRecord.task_id == task_id).first()
            rec.status = "running"
            rec.lease_until = utcnow() - timedelta(minutes=5)
            db.commit()
            rec_id = rec.id

        with test_app.session_factory() as db:
            executor._periodic_recovery(db)

        with test_app.session_factory() as db:
            task = db.get(PublishTask, task_id)
            assert task.worker_id is None
            assert task.worker_lease_until is None
            rec = db.get(PublishRecord, rec_id)
            assert rec.status == "pending"
            assert rec.lease_until is None
    finally:
        test_app.cleanup()


def test_periodic_recovery_preserves_unexpired_in_flight_work(monkeypatch):
    """判别性对照：lease 未过期的在跑记录/认领绝不能被周期恢复误伤。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.worker import executor

        task_id = _create_publishable_task(test_app)
        future = utcnow() + timedelta(minutes=5)
        with test_app.session_factory() as db:
            task = db.get(PublishTask, task_id)
            task.worker_id = "live-worker-periodic"
            task.worker_lease_until = future
            rec = db.query(PublishRecord).filter(PublishRecord.task_id == task_id).first()
            rec.status = "running"
            rec.lease_until = future
            db.commit()
            rec_id = rec.id

        with test_app.session_factory() as db:
            executor._periodic_recovery(db)

        with test_app.session_factory() as db:
            task = db.get(PublishTask, task_id)
            assert task.worker_id == "live-worker-periodic"
            assert task.worker_lease_until is not None
            rec = db.get(PublishRecord, rec_id)
            assert rec.status == "running"
            assert rec.lease_until is not None
    finally:
        test_app.cleanup()
