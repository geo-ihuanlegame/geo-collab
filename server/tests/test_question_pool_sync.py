"""问题池定时镜像同步 run_sync_once：池筛选 + 单池失败隔离 + last_sync_error。

飞书 mock，不跑真实 sleep、不打真实飞书。
"""

from server.tests.utils import build_test_app


def _admin_id(session_factory) -> int:
    from server.app.modules.system.models import User

    with session_factory() as db:
        return db.query(User).first().id


def test_run_sync_once_syncs_eligible_pools_and_isolates_failures(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation.models import QuestionItem, QuestionPool
        from server.app.modules.ai_generation.sync_scheduler import run_sync_once
        from server.app.shared.feishu_bitable import FeishuError

        uid = _admin_id(app.session_factory)
        with app.session_factory() as db:
            ok = QuestionPool(
                user_id=uid,
                name="ok",
                feishu_app_token="OK",
                feishu_table_id="t",
                auto_sync_enabled=True,
            )
            bad = QuestionPool(
                user_id=uid,
                name="bad",
                feishu_app_token="BAD",
                feishu_table_id="t",
                auto_sync_enabled=True,
            )
            off = QuestionPool(  # 关闭自动同步 → 跳过
                user_id=uid,
                name="off",
                feishu_app_token="OK",
                feishu_table_id="t",
                auto_sync_enabled=False,
            )
            nofeishu = QuestionPool(  # 未绑定飞书 → 跳过
                user_id=uid, name="nofeishu", auto_sync_enabled=True
            )
            db.add_all([ok, bad, off, nofeishu])
            db.flush()
            ok_id, bad_id, off_id = ok.id, bad.id, off.id
            db.commit()

        def _fake(app_token, table_id):
            if app_token == "BAD":
                raise FeishuError("boom")
            return [{"record_id": "r1", "fields": {"提问词": "q1"}}]

        monkeypatch.setattr("server.app.shared.feishu_bitable.list_bitable_records", _fake)

        result = run_sync_once(app.session_factory)

        # 仅 ok + bad 参与（off 关闭、nofeishu 无飞书 都被排除）
        assert result == {"pools": 2, "synced": 1, "failed": 1}

        with app.session_factory() as db:
            assert db.query(QuestionItem).filter_by(pool_id=ok_id).count() == 1
            bad_pool = db.get(QuestionPool, bad_id)
            assert bad_pool.last_sync_error and "boom" in bad_pool.last_sync_error
            # 成功池清掉了 last_sync_error
            assert db.get(QuestionPool, ok_id).last_sync_error is None
            # 关闭自动同步的池没被碰
            assert db.query(QuestionItem).filter_by(pool_id=off_id).count() == 0
    finally:
        app.cleanup()
