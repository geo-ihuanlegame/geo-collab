"""Task 4 Step 3-4 —— pipeline / scheme 运行等并发槽超时即置 failed，不无限阻塞（#9）。

全局并发闸换成 ObservableGate 后，run 入口改用 `acquire(timeout)`：闸满且超时 → 该 run 置
failed（写「等待槽位超时」），而非旧 `with Semaphore` 的无限阻塞（慢 run 占槽 ~25min）。
用填满的 capacity=1 闸 + 极小超时确定性触发，并断言超时 run 不泄漏/误占闸槽。
"""

from __future__ import annotations

import pytest

from server.app.shared.concurrency import ObservableGate
from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_pipeline_run_failed_when_slot_acquire_times_out(monkeypatch):
    from server.app.modules.pipelines import executor as pexec
    from server.app.modules.pipelines.models import Pipeline, PipelineRun
    from server.app.modules.system.models import User

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            uid = db.query(User).first().id
            p = Pipeline(user_id=uid, name="gate", has_draft=False)
            db.add(p)
            db.flush()
            run = PipelineRun(
                pipeline_id=p.id, user_id=uid, status="pending", node_results={}, article_ids=[]
            )
            db.add(run)
            db.commit()
            run_id = run.id

        full_gate = ObservableGate(1, name="pipeline")
        assert full_gate.try_acquire() is True  # 占满唯一槽
        monkeypatch.setattr(pexec, "_RUN_GATE", full_gate)
        monkeypatch.setattr(pexec, "_run_acquire_timeout", lambda: 0.05)

        pexec.run_pipeline(run_id, test_app.session_factory)

        with test_app.session_factory() as db:
            run = db.get(PipelineRun, run_id)
            assert run.status == "failed"
            assert "槽位" in (run.error_message or "")
            assert run.completed_at is not None
        # 超时 run 既没拿到槽、也没误放：闸里仍只有测试持有的那 1 个
        assert full_gate.in_use == 1
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_scheme_run_failed_when_slot_acquire_times_out(monkeypatch):
    from server.app.modules.ai_generation import scheme_executor as sexec
    from server.app.modules.ai_generation.models import (
        GenerationScheme,
        GenerationSchemeRun,
        QuestionPool,
    )
    from server.app.modules.system.models import User

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            uid = db.query(User).first().id
            pool = QuestionPool(user_id=uid, name="池")
            db.add(pool)
            db.flush()
            scheme = GenerationScheme(user_id=uid, pool_id=pool.id, name="方案")
            db.add(scheme)
            db.flush()
            run = GenerationSchemeRun(
                scheme_id=scheme.id, user_id=uid, status="pending", article_ids=[]
            )
            db.add(run)
            db.commit()
            run_id = run.id

        full_gate = ObservableGate(1, name="scheme")
        assert full_gate.try_acquire() is True
        monkeypatch.setattr(sexec, "_RUN_GATE", full_gate)
        monkeypatch.setattr(sexec, "_run_acquire_timeout", lambda: 0.05)

        sexec.run_scheme(run_id, test_app.session_factory)

        with test_app.session_factory() as db:
            run = db.get(GenerationSchemeRun, run_id)
            assert run.status == "failed"
            assert "槽位" in (run.error_message or "")
            assert run.completed_at is not None
        assert full_gate.in_use == 1
    finally:
        test_app.cleanup()
