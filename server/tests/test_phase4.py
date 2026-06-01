"""
Phase 4 feature tests:
- 4.4 ToutiaoUserInputRequired.error_type 分类
- 4.5 recover_stuck_records 写 TaskLog
- 4.1 resolve-user-input / manual-confirm 端点可访问
- 4.3 zombie session 检测函数存在且可调用
"""
from datetime import timedelta
from io import BytesIO

import pytest

from server.app.core.time import utcnow
from server.app.modules.tasks.models import PublishRecord, PublishTask, TaskLog
from server.app.modules.tasks import recover_stuck_records
from server.app.modules.tasks.drivers.toutiao import (
    ToutiaoPublishError,
    ToutiaoUserInputRequired,
    QR_HINTS,
    CAPTCHA_HINTS,
    LOGIN_REDIRECT_HINTS,
)
from server.tests.utils import build_test_app


# ── helpers ──────────────────────────────────────────────────────────────────

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _create_article(client, title="Test") -> int:
    cover = client.post("/api/assets", files={"file": ("c.png", BytesIO(_PNG), "image/png")}).json()["id"]
    return client.post("/api/articles", json={
        "title": title, "content_json": {"type": "doc", "content": []},
        "plain_text": "body", "cover_asset_id": cover,
    }).json()["id"]


def _create_account(test_app, key="acc-p4") -> int:
    d = test_app.data_dir / "browser_states" / "toutiao" / key
    d.mkdir(parents=True, exist_ok=True)
    (d / "storage_state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    return test_app.client.post(
        "/api/accounts/toutiao/login",
        json={"display_name": "Acc P4", "account_key": key, "use_browser": False},
    ).json()["id"]


def _create_task(client, article_id: int, account_id: int) -> dict:
    return client.post("/api/tasks", json={
        "name": "P4 task",
        "task_type": "single",
        "article_id": article_id,
        "accounts": [{"account_id": account_id}],
    }).json()


# ── 4.4: ToutiaoUserInputRequired.error_type ─────────────────────────────────

class TestErrorTypeClassification:
    def test_default_error_type_is_login_required(self):
        exc = ToutiaoUserInputRequired("test")
        assert exc.error_type == "login_required"

    def test_explicit_qr_scan_type(self):
        exc = ToutiaoUserInputRequired("test", error_type="qr_scan_required")
        assert exc.error_type == "qr_scan_required"

    def test_explicit_captcha_type(self):
        exc = ToutiaoUserInputRequired("test", error_type="captcha_required")
        assert exc.error_type == "captcha_required"

    def test_is_subclass_of_ToutiaoPublishError(self):
        exc = ToutiaoUserInputRequired("test", error_type="login_required")
        assert isinstance(exc, ToutiaoPublishError)

    def test_hint_groups_are_non_overlapping(self):
        qr = set(QR_HINTS)
        cap = set(CAPTCHA_HINTS)
        log = set(LOGIN_REDIRECT_HINTS)
        assert qr.isdisjoint(cap), "QR and captcha hints overlap"
        assert qr.isdisjoint(log), "QR and login hints overlap"
        assert cap.isdisjoint(log), "captcha and login hints overlap"

    def test_error_type_propagates_in_finish_record_future(self, monkeypatch):
        """_finish_record_future 应把 error_type 标签写进日志消息。"""
        test_app = build_test_app(monkeypatch)
        try:
            article_id = _create_article(test_app.client)
            account_id = _create_account(test_app)
            task_data = _create_task(test_app.client, article_id, account_id)
            task_id = task_data["id"]

            monkeypatch.setattr(
                "server.app.modules.tasks.executor.build_publish_runner_for_record",
                lambda _r: (lambda article, account, *, stop_before_publish=False: (_ for _ in ()).throw(
                    ToutiaoUserInputRequired("需要扫码", error_type="qr_scan_required")
                )),
            )

            import time as _time
            test_app.client.post(f"/api/tasks/{task_id}/execute")
            deadline = _time.time() + 5.0
            while _time.time() < deadline:
                records = test_app.client.get(f"/api/tasks/{task_id}/records").json()
                if records and records[0]["status"] == "waiting_user_input":
                    break
                _time.sleep(0.05)

            logs = test_app.client.get(f"/api/tasks/{task_id}/logs").json()
            assert any("扫码" in lg["message"] or "qr_scan" in lg["message"].lower() for lg in logs), \
                f"Expected qr_scan hint in logs, got: {[lg['message'] for lg in logs]}"
        finally:
            test_app.cleanup()


# ── 4.5: recover_stuck_records 写 TaskLog ────────────────────────────────────

class TestRecoverStuckRecordsLogging:
    def test_recovery_adds_task_log(self, monkeypatch):
        """卡住的 record 重置为 pending 时，必须写入 TaskLog。"""
        test_app = build_test_app(monkeypatch)
        try:
            article_id = _create_article(test_app.client)
            account_id = _create_account(test_app, key="acc-recover")
            task_data = _create_task(test_app.client, article_id, account_id)
            task_id = task_data["id"]

            # 手动把 record 置为 running + expired lease
            with test_app.session_factory() as db:
                records = db.query(PublishRecord).filter(PublishRecord.task_id == task_id).all()
                assert records, "Task should have at least one record"
                record_id = records[0].id
                records[0].status = "running"
                records[0].lease_until = utcnow() - timedelta(seconds=10)
                db.commit()

            # 调用恢复
            with test_app.session_factory() as db:
                recover_stuck_records(db)

            # 验证 record 已重置
            with test_app.session_factory() as db:
                record = db.get(PublishRecord, record_id)
                assert record is not None
                assert record.status == "pending"
                assert record.lease_until is None

                # 验证 TaskLog 已写入
                logs = db.query(TaskLog).filter(
                    TaskLog.task_id == task_id,
                    TaskLog.record_id == record_id,
                ).all()
                assert logs, "Expected TaskLog to be written for recovered record"
                assert any("重启" in lg.message or "重置" in lg.message for lg in logs), \
                    f"Expected recovery message in log, got: {[lg.message for lg in logs]}"
                assert all(lg.level == "warn" for lg in logs), "Recovery logs should be 'warn' level"
        finally:
            test_app.cleanup()

    def test_no_log_when_no_stuck_records(self, monkeypatch):
        """没有卡住的 record 时，不写 TaskLog。"""
        test_app = build_test_app(monkeypatch)
        try:
            with test_app.session_factory() as db:
                log_count_before = db.query(TaskLog).count()
                recover_stuck_records(db)
                log_count_after = db.query(TaskLog).count()
            assert log_count_before == log_count_after
        finally:
            test_app.cleanup()


# ── 4.1: resolve-user-input / manual-confirm API endpoints ───────────────────

class TestManualInterventionEndpoints:
    def _setup_waiting_record(self, monkeypatch, status: str):
        """创建一个处于指定状态的 record，返回 (test_app, record_id, task_id)。"""
        test_app = build_test_app(monkeypatch)
        article_id = _create_article(test_app.client)
        account_id = _create_account(test_app, key=f"acc-{status[:4]}")
        task_data = _create_task(test_app.client, article_id, account_id)
        task_id = task_data["id"]

        with test_app.session_factory() as db:
            records = db.query(PublishRecord).filter(PublishRecord.task_id == task_id).all()
            record_id = records[0].id
            records[0].status = status
            records[0].lease_until = None
            db.commit()

        return test_app, record_id, task_id

    def test_resolve_user_input_resets_to_pending(self, monkeypatch):
        test_app, record_id, task_id = self._setup_waiting_record(monkeypatch, "waiting_user_input")
        try:
            monkeypatch.setattr(
                "server.app.modules.tasks.executor.build_publish_runner_for_record",
                lambda _r: (lambda article, account, *, stop_before_publish=False: (_ for _ in ()).throw(
                    Exception("stop immediately")
                )),
            )
            resp = test_app.client.post(f"/api/publish-records/{record_id}/resolve-user-input")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            # resolve resets to pending, then background may move it to running/failed
            assert data["status"] in ("pending", "running", "failed")
        finally:
            test_app.cleanup()

    def test_manual_confirm_succeeded(self, monkeypatch):
        test_app, record_id, task_id = self._setup_waiting_record(monkeypatch, "waiting_manual_publish")
        try:
            resp = test_app.client.post(
                f"/api/publish-records/{record_id}/manual-confirm",
                json={"outcome": "succeeded"},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "succeeded"
        finally:
            test_app.cleanup()

    def test_manual_confirm_failed(self, monkeypatch):
        test_app, record_id, task_id = self._setup_waiting_record(monkeypatch, "waiting_manual_publish")
        try:
            resp = test_app.client.post(
                f"/api/publish-records/{record_id}/manual-confirm",
                json={"outcome": "failed"},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "failed"
        finally:
            test_app.cleanup()

    def test_unauthenticated_cannot_resolve(self, monkeypatch):
        from fastapi.testclient import TestClient
        test_app, record_id, _ = self._setup_waiting_record(monkeypatch, "waiting_user_input")
        try:
            anon = TestClient(test_app.client.app)
            resp = anon.post(f"/api/publish-records/{record_id}/resolve-user-input")
            assert resp.status_code == 401
        finally:
            test_app.cleanup()


# ── 4.3: zombie session detection ────────────────────────────────────────────

class TestZombieSessionDetection:
    def test_cleanup_zombie_sessions_is_callable(self):
        """_cleanup_zombie_sessions 函数存在且在没有活动 session 时不抛出。"""
        from server.app.modules.accounts.browser import _cleanup_zombie_sessions
        _cleanup_zombie_sessions()  # should not raise

    def test_cleanup_zombie_sessions_skips_healthy_sessions(self, monkeypatch):
        """有活跃但进程健康的 session 时不应误清理。"""
        from server.app.modules.accounts import browser as bs
        import subprocess
        from dataclasses import dataclass, field
        from pathlib import Path

        # 构造一个"健康"的假 session（进程 poll() 返回 None 表示还在运行）
        class FakeProcess:
            def poll(self):
                return None  # 仍在运行

        @dataclass
        class FakeManagedProcess:
            name: str
            process: object
            log_handle: object = None

        @dataclass
        class FakeSession:
            id: str
            account_key: str
            display_number: int
            display: str
            vnc_port: int
            novnc_port: int
            novnc_url: str
            log_dir: Path
            processes: list
            started_at: float

        session = FakeSession(
            id="healthy01",
            account_key="test",
            display_number=99,
            display=":99",
            vnc_port=5999,
            novnc_port=6999,
            novnc_url="http://localhost:6999/vnc.html",
            log_dir=Path("/tmp"),
            processes=[FakeManagedProcess("xvfb", FakeProcess())],
            started_at=0.0,
        )

        original = dict(bs._active_sessions)
        bs._active_sessions["healthy01"] = session
        try:
            bs._cleanup_zombie_sessions()
            # healthy session should NOT be removed
            assert "healthy01" in bs._active_sessions
        finally:
            bs._active_sessions.clear()
            bs._active_sessions.update(original)
