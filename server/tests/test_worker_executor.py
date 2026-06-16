from datetime import timedelta
from io import BytesIO

from server.app.core.time import utcnow
from server.app.modules.system.models import WorkerHeartbeat
from server.app.modules.tasks import recover_stuck_task_claims
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
            "title": "Worker Claim Article",
            "content_json": {"type": "doc", "content": []},
            "plain_text": "body",
            "cover_asset_id": cover,
        },
    ).json()
    state_dir = test_app.data_dir / "browser_states" / "toutiao" / "worker-claim"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "storage_state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    account = client.post(
        "/api/accounts/toutiao/login",
        json={"display_name": "Worker Claim", "account_key": "worker-claim", "use_browser": False},
    ).json()
    task = client.post(
        "/api/tasks",
        json={
            "name": "worker claim",
            "task_type": "single",
            "article_id": article["id"],
            "accounts": [{"account_id": account["id"]}],
        },
    ).json()
    return task["id"]


def test_production_execute_leaves_task_for_worker_claim(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.tasks import router as task_routes
        from server.worker import executor

        monkeypatch.setattr(task_routes, "bg_session_factory", None)
        task_id = _create_publishable_task(test_app)

        response = test_app.client.post(f"/api/tasks/{task_id}/execute")
        assert response.status_code == 202
        assert response.json() == {"queued": True}

        with test_app.session_factory() as db:
            claimed = executor._claim_next_task(db)
            assert claimed is not None
            assert claimed.id == task_id
            assert claimed.worker_id == executor.WORKER_ID
            assert claimed.worker_heartbeat_at is not None
            executor._release_task_claim(db, task_id)
    finally:
        test_app.cleanup()


# ── recover_stuck_task_claims：worker 崩溃后释放过期认领 ──────────────────────────
# 与 recover_stuck_records（test_phase4）对称：worker 崩溃使 worker_lease_until 过期后，
# 必须清空 worker_id 让别的 worker 重抢；但有租约保护——未过期的认领绝不能动。


def test_recover_releases_expired_worker_claim(monkeypatch):
    """worker_lease_until 已过期的认领被清空（worker_id / lease / heartbeat 全部归零）。"""
    test_app = build_test_app(monkeypatch)
    try:
        task_id = _create_publishable_task(test_app)
        with test_app.session_factory() as db:
            task = db.get(PublishTask, task_id)
            task.worker_id = "dead-worker-123"
            task.worker_lease_until = utcnow() - timedelta(minutes=5)
            task.worker_heartbeat_at = utcnow() - timedelta(minutes=5)
            db.commit()

        with test_app.session_factory() as db:
            recover_stuck_task_claims(db)

        with test_app.session_factory() as db:
            task = db.get(PublishTask, task_id)
            assert task.worker_id is None
            assert task.worker_lease_until is None
            assert task.worker_heartbeat_at is None
    finally:
        test_app.cleanup()


def test_recover_preserves_unexpired_worker_claim(monkeypatch):
    """租约未过期的认领绝不能被释放——否则会把别的 worker 正在跑的任务从它手里抢走。

    这是与 expired 用例对称的判别性测试：若实现漏掉 worker_lease_until < now 条件，本断言即红。
    """
    test_app = build_test_app(monkeypatch)
    try:
        task_id = _create_publishable_task(test_app)
        future = utcnow() + timedelta(minutes=5)
        with test_app.session_factory() as db:
            task = db.get(PublishTask, task_id)
            task.worker_id = "live-worker-456"
            task.worker_lease_until = future
            db.commit()

        with test_app.session_factory() as db:
            recover_stuck_task_claims(db)

        with test_app.session_factory() as db:
            task = db.get(PublishTask, task_id)
            assert task.worker_id == "live-worker-456"
            assert task.worker_lease_until is not None
    finally:
        test_app.cleanup()


def test_recover_ignores_unclaimed_task(monkeypatch):
    """没有 worker 认领（worker_id 为 None）的任务不受影响、不报错。"""
    test_app = build_test_app(monkeypatch)
    try:
        task_id = _create_publishable_task(test_app)
        with test_app.session_factory() as db:
            recover_stuck_task_claims(db)

        with test_app.session_factory() as db:
            task = db.get(PublishTask, task_id)
            assert task.worker_id is None
    finally:
        test_app.cleanup()


# ── worker/executor.py 关键函数：claim 竞态 / 释放归属 / 心跳 / 卡死任务复位 ─────────


def test_claim_skips_task_already_claimed_by_another_worker(monkeypatch):
    """已被别的 worker 抢占（worker_id 非空）的任务，_claim_next_task 不再返回它。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.worker import executor

        task_id = _create_publishable_task(test_app)
        with test_app.session_factory() as db:
            task = db.get(PublishTask, task_id)
            task.worker_id = "other-worker-789"
            task.worker_lease_until = utcnow() + timedelta(minutes=5)
            db.commit()

        with test_app.session_factory() as db:
            claimed = executor._claim_next_task(db)
            assert claimed is None
    finally:
        test_app.cleanup()


def test_release_does_not_touch_another_workers_claim(monkeypatch):
    """_release_task_claim 只释放本 worker 的认领；别的 worker 的认领原样保留。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.worker import executor

        task_id = _create_publishable_task(test_app)
        with test_app.session_factory() as db:
            task = db.get(PublishTask, task_id)
            task.worker_id = "other-worker-789"
            task.worker_lease_until = utcnow() + timedelta(minutes=5)
            db.commit()

        with test_app.session_factory() as db:
            executor._release_task_claim(db, task_id)

        with test_app.session_factory() as db:
            task = db.get(PublishTask, task_id)
            assert task.worker_id == "other-worker-789"
    finally:
        test_app.cleanup()


def test_write_worker_heartbeat_upserts_single_row(monkeypatch):
    """_write_worker_heartbeat 为本 WORKER_ID 写心跳；重复调用是 upsert（merge），不产生多行。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.worker import executor

        with test_app.session_factory() as db:
            executor._write_worker_heartbeat(db)

        with test_app.session_factory() as db:
            rows = db.query(WorkerHeartbeat).filter_by(worker_id=executor.WORKER_ID).all()
            assert len(rows) == 1
            first_beat = rows[0].heartbeat_at

        with test_app.session_factory() as db:
            executor._write_worker_heartbeat(db)

        with test_app.session_factory() as db:
            rows = db.query(WorkerHeartbeat).filter_by(worker_id=executor.WORKER_ID).all()
            assert len(rows) == 1, "重复心跳应 upsert，不应新增行"
            assert rows[0].heartbeat_at >= first_beat
    finally:
        test_app.cleanup()


def test_check_stuck_tasks_collapses_running_task_with_terminal_records(monkeypatch):
    """task 卡在 running、无 worker 认领、但所有 record 已终态 → 被收口到终态。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.worker import executor

        task_id = _create_publishable_task(test_app)
        with test_app.session_factory() as db:
            task = db.get(PublishTask, task_id)
            task.status = "running"
            task.worker_id = None
            for rec in db.query(PublishRecord).filter(PublishRecord.task_id == task_id).all():
                rec.status = "succeeded"
            db.commit()

        with test_app.session_factory() as db:
            executor._check_stuck_tasks(db)

        with test_app.session_factory() as db:
            task = db.get(PublishTask, task_id)
            assert task.status == "succeeded"
    finally:
        test_app.cleanup()


def test_check_stuck_tasks_leaves_running_task_with_pending_record(monkeypatch):
    """仍有未终态 record（pending）的 running 任务不收口——判别性对照，防止误把活任务标终态。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.worker import executor

        task_id = _create_publishable_task(test_app)
        with test_app.session_factory() as db:
            task = db.get(PublishTask, task_id)
            task.status = "running"
            task.worker_id = None
            # record 保持默认 pending
            db.commit()

        with test_app.session_factory() as db:
            executor._check_stuck_tasks(db)

        with test_app.session_factory() as db:
            task = db.get(PublishTask, task_id)
            assert task.status == "running"
    finally:
        test_app.cleanup()


def test_startup_recovers_claims_and_writes_heartbeat(monkeypatch):
    """_startup 释放过期认领、复位卡死 record、并写下首个心跳。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.worker import executor

        task_id = _create_publishable_task(test_app)
        with test_app.session_factory() as db:
            task = db.get(PublishTask, task_id)
            task.worker_id = "dead-worker-on-restart"
            task.worker_lease_until = utcnow() - timedelta(minutes=5)
            rec = db.query(PublishRecord).filter(PublishRecord.task_id == task_id).first()
            rec.status = "running"
            rec.lease_until = utcnow() - timedelta(minutes=5)
            db.commit()
            rec_id = rec.id

        with test_app.session_factory() as db:
            executor._startup(db)

        with test_app.session_factory() as db:
            task = db.get(PublishTask, task_id)
            assert task.worker_id is None
            rec = db.get(PublishRecord, rec_id)
            assert rec.status == "pending"
            assert db.query(WorkerHeartbeat).filter_by(worker_id=executor.WORKER_ID).count() == 1
    finally:
        test_app.cleanup()
