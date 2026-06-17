"""Task 6（封堵 #5）：方案运行活跃去重 + 有界派发线程 —— 行为契约。

两条修复（镜像 pipeline）：
1. **活跃去重**：同一 scheme 已有 pending/running 运行时，再建 run 抛 ConflictError（→ 409）。
   判定与 recover_stuck_scheme_runs 一致（status ∈ {pending, running}），在 scheme 行锁内做，
   与并发 POST 安全。封堵「连点运行 = 重复 run」。
2. **有界派发**：路由不再裸 `threading.Thread` 无界 spawn，而是提交到有界 `_DISPATCH_POOL`
   （ThreadPoolExecutor），跨 scheme 的并发 POST 也不会炸出无限线程。
"""

from __future__ import annotations

import threading

import pytest

from server.tests.utils import build_test_app


def _make_scheme(app) -> int:
    """建最小可运行方案：池 + 1 问题(category A) + 1 启用 generation 模板 + 1 行。返回 scheme_id。"""
    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool
    from server.app.modules.prompt_templates.models import PromptTemplate
    from server.app.modules.system.models import User

    with app.session_factory() as db:
        uid = db.query(User).first().id
        pool = QuestionPool(user_id=uid, name="P")
        db.add(pool)
        db.flush()
        item = QuestionItem(
            pool_id=pool.id,
            record_id="a1",
            fields={},
            question_text="问题-a1",
            category="A",
            source_active=True,
        )
        db.add(item)
        tpl = PromptTemplate(
            name="g", content="写：{{问题}}", scope="generation", user_id=uid, is_enabled=True
        )
        db.add(tpl)
        db.commit()
        pool_id, item_id, tpl_id = pool.id, item.id, tpl.id

    body = {
        "name": "方案-dedup",
        "pool_id": pool_id,
        "lines": [
            {
                "question_type": "A",
                "question_item_ids": [item_id],
                "article_count": 1,
                "allowed_prompt_template_ids": [tpl_id],
            }
        ],
    }
    r = app.client.post("/api/generation/schemes", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ── 活跃去重 ───────────────────────────────────────────────────────────────────
@pytest.mark.mysql
def test_create_run_rejects_when_active_run_exists(monkeypatch):
    """create_run：同一 scheme 已有 pending run 时抛 ConflictError（镜像 pipeline create_run）。"""
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation import scheme_executor as se
        from server.app.modules.ai_generation.scheme_service import get_scheme
        from server.app.shared.errors import ConflictError

        sid = _make_scheme(app)
        with app.session_factory() as db:
            scheme = get_scheme(db, sid)
            uid = scheme.user_id
            se.create_run(db, scheme=scheme, user_id=uid)
            db.commit()
            scheme = get_scheme(db, sid)
            with pytest.raises(ConflictError):
                se.create_run(db, scheme=scheme, user_id=uid)
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_create_run_allowed_after_previous_finished(monkeypatch):
    """非活跃（done/failed）的历史 run 不阻塞新 run —— 去重只挡在途，不挡历史。"""
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation import scheme_executor as se
        from server.app.modules.ai_generation.scheme_service import get_scheme

        sid = _make_scheme(app)
        with app.session_factory() as db:
            scheme = get_scheme(db, sid)
            uid = scheme.user_id
            run1 = se.create_run(db, scheme=scheme, user_id=uid)
            run1.status = "done"  # 模拟已完成
            db.commit()
            run1_id = run1.id
            scheme = get_scheme(db, sid)
            run2 = se.create_run(db, scheme=scheme, user_id=uid)  # 不应抛
            db.commit()
            assert run2.id != run1_id
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_post_run_twice_second_returns_409(monkeypatch):
    """端到端：连点同一 scheme 运行，第二次 409；DB 里只留一条 run。"""
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation import scheme_executor as se
        from server.app.modules.ai_generation.models import GenerationSchemeRun

        # run_scheme 置 no-op：第一条 run 保持 pending（不真生文），稳定复现"已有活跃 run"
        monkeypatch.setattr(se, "run_scheme", lambda *a, **k: None)

        sid = _make_scheme(app)
        r1 = app.client.post(f"/api/generation/schemes/{sid}/runs")
        assert r1.status_code == 202, r1.text
        r2 = app.client.post(f"/api/generation/schemes/{sid}/runs")
        assert r2.status_code == 409, r2.text

        with app.session_factory() as db:
            cnt = db.query(GenerationSchemeRun).filter(GenerationSchemeRun.scheme_id == sid).count()
        assert cnt == 1, f"应只创建 1 条 run，实际 {cnt}"
    finally:
        app.cleanup()


# ── 有界派发 ───────────────────────────────────────────────────────────────────
def test_dispatch_pool_is_bounded():
    """派发不再是裸 Thread：存在有界 ThreadPoolExecutor（封堵 #5 无界 spawn）。"""
    from concurrent.futures import ThreadPoolExecutor

    from server.app.modules.ai_generation import scheme_executor as se

    assert isinstance(se._DISPATCH_POOL, ThreadPoolExecutor)
    assert 1 <= se._DISPATCH_POOL._max_workers <= 64  # 有界，非无限


def test_submit_scheme_run_executes_via_pool(monkeypatch):
    """submit_scheme_run 经有界池真正执行 run_scheme（dispatch 路径连通、跑在派发池线程上）。"""
    from server.app.modules.ai_generation import scheme_executor as se

    done = threading.Event()
    seen: dict = {}

    def fake_run_scheme(run_id, factory):
        seen["run_id"] = run_id
        seen["thread"] = threading.current_thread().name
        done.set()

    monkeypatch.setattr(se, "run_scheme", fake_run_scheme)
    se.submit_scheme_run(4242, lambda: None)

    assert done.wait(timeout=5), "submit_scheme_run 未在有界池里执行 run_scheme"
    assert seen["run_id"] == 4242
    assert "scheme-dispatch" in seen["thread"]
