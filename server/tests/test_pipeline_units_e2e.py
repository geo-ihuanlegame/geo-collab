"""端到端回归：问题源 per-type「模板/数量」经 save_draft→publish_draft→create_run→执行器
全链路后，仍被 ai_generate 逐单元采用（而非回退到本节点兜底）。

现有 test_ai_generate_units / test_question_source_units 直接调 handler，跳过了
草稿→发布→运行快照这段序列化往返；本测试专门覆盖那段未被验证的边界（见排查计划）。
"""

import uuid

import pytest

from server.tests.utils import build_test_app

UNIT_TPL = "UNIT-TPL: {{question}}"
FALLBACK_TPL = "FALLBACK-TPL: {{question}}"


def _make_tpl(app, uid, content):
    from server.app.modules.prompt_templates.models import PromptTemplate

    with app.session_factory() as db:
        t = PromptTemplate(
            name=f"tpl-{uuid.uuid4().hex[:6]}",
            content=content,
            scope="generation",
            user_id=uid,
            is_enabled=True,
        )
        db.add(t)
        db.commit()
        return t.id


def _make_pool(app, uid, items):
    """items: list[(record_id, category, text)]。返回 pool_id。"""
    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool

    with app.session_factory() as db:
        pool = QuestionPool(user_id=uid, name="池")
        db.add(pool)
        db.flush()
        for rid, cat, text in items:
            db.add(
                QuestionItem(
                    pool_id=pool.id,
                    record_id=rid,
                    fields={},
                    category=cat,
                    question_text=text,
                    source_active=True,
                )
            )
        db.commit()
        return pool.id


@pytest.mark.mysql
def test_per_type_units_survive_publish_and_run(monkeypatch):
    # 记录每次生文实际拿到的模板内容，用以区分 per-type 模板 vs 兜底模板。
    used_templates: list[str] = []

    def _stub_generate(*, session_factory, user_id, template_content, question_text, model=None):
        from server.app.modules.articles.schemas import ArticleCreate
        from server.app.modules.articles.service import create_article

        used_templates.append(template_content)
        db = session_factory()
        try:
            art = create_article(
                db,
                user_id,
                ArticleCreate(
                    title=f"A-{uuid.uuid4().hex[:6]}",
                    content_json={"type": "doc", "content": []},
                    content_html="<p>x</p>",
                    plain_text="x",
                    word_count=1,
                    client_request_id=str(uuid.uuid4()),
                ),
            )
            db.commit()
            return art.id
        finally:
            db.close()

    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_generate_node.generate_article_from_prompt",
        _stub_generate,
    )

    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines import service as svc
        from server.app.modules.pipelines.executor import _run_pipeline_inner, create_run
        from server.app.modules.pipelines.models import PipelineRun
        from server.app.modules.system.models import User

        with app.session_factory() as db:
            uid = db.query(User).first().id

        t_unit = _make_tpl(app, uid, UNIT_TPL)
        t_fallback = _make_tpl(app, uid, FALLBACK_TPL)
        pool_id = _make_pool(app, uid, [("r0", "美食", "红烧肉"), ("r1", "美食", "糖醋")])

        # per-type：count=2 + 模板=t_unit；ai_generate 兜底=count 5 + t_fallback。
        # 若往返链路把 per-type 丢了 → 会产出 5 篇且用 FALLBACK_TPL；正确则 2 篇且 UNIT_TPL。
        snapshot = {
            "schemaVersion": 1,
            "nodes": [
                {
                    "node_type": "question_source",
                    "name": "问题源",
                    "node_index": 0,
                    "config": {
                        "pool_id": pool_id,
                        "units": [
                            {
                                "question_type": "美食",
                                "record_ids": ["r0", "r1"],
                                "allowed_prompt_template_ids": [t_unit],
                                "article_count": 2,
                            }
                        ],
                    },
                    "flow_meta": None,
                },
                {
                    "node_type": "ai_generate",
                    "name": "AI生文",
                    "node_index": 1,
                    "config": {
                        "prompt_template_id": t_fallback,
                        "count": 5,
                        "model": None,
                    },
                    "flow_meta": None,
                },
            ],
        }

        with app.session_factory() as db:
            p = svc.create_pipeline(db, user_id=uid, name="e2e-units", description=None)
            svc.save_draft(db, p, snapshot)
            svc.publish_draft(db, p, remark=None, user_id=uid)
            db.commit()
            pid = p.id

        with app.session_factory() as db:
            run = create_run(db, pipeline_id=pid, user_id=uid)
            db.commit()
            run_id = run.id

        _run_pipeline_inner(run_id, app.session_factory)

        with app.session_factory() as db:
            run = db.get(PipelineRun, run_id)
            assert run.status == "done", f"status={run.status} err={run.error_message}"
            assert len(run.article_ids) == 2  # per-type count=2，而非兜底 5

        # 每篇都用了 per-type 模板（t_unit），没有回退到 ai_generate 的兜底模板。
        assert used_templates == [UNIT_TPL, UNIT_TPL], used_templates
    finally:
        app.cleanup()
