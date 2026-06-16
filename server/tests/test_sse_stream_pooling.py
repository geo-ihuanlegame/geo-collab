"""SSE 任务流的连接池契约：流必须「每个轮询周期开/关 session」，不得跨 time.sleep 长持 DB 连接。

为何重要：旧实现把 `sess` 开在 while 循环外、整条流期间（含 sleep）一直占着一条连接。
多人观看任务时几条流就占满连接池（旧上限 5+10=15）→ 全站请求排队 30s 后 QueuePool 超时 = 服务
"崩溃"（连接池耗尽事故）。本测试在测试 engine 上挂 checkout/checkin 事件，统计「到达 sleep 时
仍被借出的连接数」：修好后应为 0（查询完即归还池），旧实现为 1（持着不放）——这是 red/green 分界。
"""

import pytest
from sqlalchemy import event

from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_sse_stream_releases_db_connection_between_ticks(monkeypatch):
    # 关掉后台调度线程，避免其 session 在测试 engine 上 checkout 干扰计数（双保险）。
    monkeypatch.setenv("GEO_PIPELINE_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("GEO_QUESTION_POOL_AUTO_SYNC_ENABLED", "false")
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.system.models import Platform, User
        from server.app.modules.tasks.models import PublishTask

        # 造一个非终态任务（流会进入轮询、到达 sleep）。to_task_read 需要 platform 关联。
        with app.session_factory() as db:
            admin = db.query(User).filter_by(username="testadmin").one()
            platform = Platform(code="toutiao", name="头条")
            db.add(platform)
            db.flush()
            task = PublishTask(
                user_id=admin.id,
                name="sse-pool-test",
                task_type="single",
                status="running",
                platform_id=platform.id,
            )
            db.add(task)
            db.commit()
            task_id = task.id

        # SSE 用的 SessionLocal 已被 build_test_app 绑到 app.engine；在该 engine 上数借出连接。
        checked_out = {"n": 0}

        @event.listens_for(app.engine, "checkout")
        def _on_checkout(*_args):  # noqa: ANN001
            checked_out["n"] += 1

        @event.listens_for(app.engine, "checkin")
        def _on_checkin(*_args):  # noqa: ANN001
            checked_out["n"] -= 1

        captured: list[int] = []

        import server.app.modules.tasks.router as router_mod

        class _StopStream(Exception):
            pass

        def fake_sleep(_seconds):
            # 到达 sleep 时仍被借出的连接数：修好后 SSE 已在 finally 里 close、归还池 → 0。
            captured.append(checked_out["n"])
            raise _StopStream()  # 终止流，避免真等 1s

        monkeypatch.setattr(router_mod.time, "sleep", fake_sleep)

        with app.client.stream("GET", f"/api/tasks/{task_id}/stream") as resp:
            for _ in resp.iter_lines():
                pass

        assert captured, "流未进入轮询周期（未到达 sleep）——测试前提不成立"
        assert captured[0] == 0, (
            f"SSE 在 sleep 期间仍持有 {captured[0]} 条池连接（应为 0：连接须在 sleep 前归还池）"
        )
    finally:
        app.cleanup()
