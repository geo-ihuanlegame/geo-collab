"""发布提交边界行锁竞争回归（#133 暴露的 1205）。

生产现象：头条 inpage 发布在途时偶发卡 ~50s 后失败，worker 日志顶端真实异常是
`OperationalError(1205, 'Lock wait timeout exceeded')`，栈经 `toutiao_inpage.py` 的
`with guard.committing()` → `CommitGuard.mark_pending`（独立 session 写 publish_records.
commit_attempted_at）。

根因：主执行循环 `_run_pending_records` 每轮在主 `db` session 上跑心跳
`_heartbeat_running_records`（`UPDATE publish_records SET lease_until WHERE status='running'`，
锁住在跑记录行），随后 `wait(timeout=1)` 阻塞——本轮无 future 完成时走不到唯一的 `db.commit()`，
于是主循环事务横跨整个发布过程不提交、死握行锁。发布线程在提交边界用独立 session UPDATE
同一行 → 撞锁等到 innodb_lock_wait_timeout → 1205。

本测试用一个会阻塞的 publisher 忠实复现：它在“发布在途”时（跨过主循环至少一次无 future 完成的
心跳迭代后）执行与真实 `mark_pending` 等价的提交边界写（独立 session + 短锁等待超时）。
修复前主循环握锁 → 该写 1205 → 记录失败；修复后主循环每轮 commit 释放锁 → 写成功 → 记录成功。
"""

import threading
import time as _time
from io import BytesIO

import pytest

from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql

# 1x1 PNG
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _upload_cover(client) -> str:
    resp = client.post("/api/assets", files={"file": ("cover.png", BytesIO(_PNG), "image/png")})
    assert resp.status_code == 200
    return resp.json()["id"]


def _create_article(client, cover_id: str) -> int:
    resp = client.post(
        "/api/articles",
        json={
            "title": "Article",
            "content_json": {"type": "doc", "content": []},
            "plain_text": "Body",
            "cover_asset_id": cover_id,
        },
    )
    assert resp.status_code == 200
    return resp.json()["id"]


def _create_account(client, data_dir, account_key: str) -> int:
    state_dir = data_dir / "browser_states" / "toutiao" / account_key
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "storage_state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    resp = client.post(
        "/api/accounts/toutiao/login",
        json={"display_name": "Account A", "account_key": account_key, "use_browser": False},
    )
    assert resp.status_code == 200
    return resp.json()["id"]


def test_inflight_commit_write_not_blocked_by_heartbeat_lock(monkeypatch):
    """发布在途时，主执行循环不得握 publish_records 行锁横跨 wait()——否则发布线程的提交边界写
    （commit_attempted_at）撞锁超时 1205、发布失败。"""
    from server.app.modules.tasks.drivers.toutiao import PublishFillResult

    test_app = build_test_app(monkeypatch)
    client = test_app.client

    publishing = threading.Event()
    release = threading.Event()
    write_error: list[Exception] = []

    def _runner_factory(record):
        record_id = record.id

        def _run(article, account, *, stop_before_publish=False):
            from sqlalchemy import text
            from sqlalchemy import update as sa_update

            from server.app.core.time import utcnow
            from server.app.db.session import SessionLocal
            from server.app.modules.tasks.models import PublishRecord

            # 进入“发布在途”，阻塞到测试放行——保证跨过主循环至少一次无 future 完成的心跳迭代，
            # 那一轮心跳会锁住本记录行。
            publishing.set()
            release.wait(10)

            # 等价于真实 CommitGuard.mark_pending：独立 session 写 commit_attempted_at，
            # 短锁等待超时——主循环若仍握锁，这里抛 OperationalError(1205)。
            db = SessionLocal()
            try:
                db.execute(text("SET SESSION innodb_lock_wait_timeout = 3"))
                db.execute(
                    sa_update(PublishRecord)
                    .where(PublishRecord.id == record_id)
                    .values(commit_attempted_at=utcnow())
                )
                db.commit()
            except Exception as exc:  # noqa: BLE001
                write_error.append(exc)
                raise
            finally:
                db.close()

            return PublishFillResult(
                url="https://example.com/a/1", title="Article", message="发布成功"
            )

        return _run

    try:
        monkeypatch.setattr(
            "server.app.modules.tasks.executor.build_publish_runner_for_record",
            _runner_factory,
        )
        cover_id = _upload_cover(client)
        article_id = _create_article(client, cover_id)
        account_id = _create_account(client, test_app.data_dir, "account-a")
        task = client.post(
            "/api/tasks",
            json={
                "name": "publish task",
                "platform_code": "toutiao",
                "article_id": article_id,
                "task_type": "single",
                "accounts": [{"account_id": account_id}],
                "stop_before_publish": False,
            },
        ).json()

        resp = client.post(f"/api/tasks/{task['id']}/execute")
        assert resp.status_code == 202

        assert publishing.wait(5), "publisher 未进入在途状态"
        # 让主循环跑 >=1 次“无 future 完成”的心跳迭代（wait timeout=1s），它在此锁住记录行。
        _time.sleep(1.5)
        release.set()  # 放行发布线程做提交边界写

        deadline = _time.time() + 15
        final = None
        while _time.time() < deadline:
            t = client.get(f"/api/tasks/{task['id']}").json()
            if t["status"] in ("succeeded", "failed", "partial_failed", "cancelled"):
                final = t
                break
            _time.sleep(0.05)

        assert final is not None, "任务未在期限内收口"
        assert not write_error, f"提交边界写被主循环心跳锁阻塞(1205): {write_error!r}"
        assert final["status"] == "succeeded", f"任务应成功，实际为 {final['status']}"
    finally:
        release.set()
        test_app.cleanup()
