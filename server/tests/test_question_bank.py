"""问题库（可消费队列）+ 单一 pipeline（手动按板块分组 / 自动选题）测试。

覆盖：取法、converter、同步 upsert（消费不复活 + 抽 提问词/分类板块）、
按板块分组、自动选题板块优先级/轮转/K 随机/不消费、管线两种模式集成、API。
LiteLLM 与飞书均 mock。
"""

import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from server.app.modules.ai_generation import question_bank as qb
from server.tests.utils import build_test_app


def _fake_completion(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _seed_skill(data_dir) -> str:
    skill_dir = Path(data_dir) / "skill_x"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# 测试 skill\n写简短推荐。", encoding="utf-8")
    return str(skill_dir)


def _admin_id(session_factory) -> int:
    from server.app.modules.system.models import User

    with session_factory() as db:
        return db.query(User).first().id


# ── 纯单元：默认取法 + converter ─────────────────────────────────────────────


def test_extract_question_text_flattens_fields_and_rich_text():
    fields = {
        "问题": "1.有没有无广告的游戏、2.有没有不肝不氪的良心游戏",
        "备注": [{"type": "text", "text": "融合写一篇"}],
    }
    text = qb.extract_question_text(fields)
    assert "1.有没有无广告的游戏、2.有没有不肝不氪的良心游戏" in text
    assert "融合写一篇" in text


def test_extract_question_text_empty_fields():
    assert qb.extract_question_text({}) == ""


def test_converter_markdown_to_tiptap_and_html():
    from server.app.modules.ai_generation.converter import markdown_to_html, markdown_to_tiptap

    md = "## 小标题\n\n一段正文。"
    doc = markdown_to_tiptap(md)
    assert doc["type"] == "doc"
    assert len(doc["content"]) >= 1
    assert "小标题" in markdown_to_html(md)


# ── 同步：upsert 语义（消费不复活）──────────────────────────────────────────


def test_sync_pool_upsert_does_not_resurrect_consumed(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation.models import QuestionItem, QuestionPool

        uid = _admin_id(app.session_factory)
        with app.session_factory() as db:
            pool = QuestionPool(
                user_id=uid, name="p", feishu_app_token="app", feishu_table_id="tbl"
            )
            db.add(pool)
            db.flush()
            # 已消费的 rec1（应被同步跳过、不复活、不覆盖）
            db.add(
                QuestionItem(
                    pool_id=pool.id,
                    record_id="rec1",
                    fields={"q": "old"},
                    status="consumed",
                    article_id=None,
                )
            )
            db.commit()
            pool_id = pool.id

        monkeypatch.setattr(
            "server.app.shared.feishu_bitable.list_bitable_records",
            lambda app_token, table_id: [
                {"record_id": "rec1", "fields": {"q": "NEW"}},
                {"record_id": "rec2", "fields": {"q": "two"}},
            ],
        )
        with app.session_factory() as db:
            pool = db.get(QuestionPool, pool_id)
            res = qb.sync_pool(db, pool)
            db.commit()

        assert res["added"] == 1  # rec2 新增
        assert res["skipped_consumed"] == 1  # rec1 不复活
        with app.session_factory() as db:
            items = {it.record_id: it for it in db.query(QuestionItem).filter_by(pool_id=pool_id)}
            assert items["rec1"].status == "consumed"
            assert items["rec1"].fields["q"] == "old"  # 未被覆盖
            assert items["rec2"].status == "pending"
    finally:
        app.cleanup()


# ── 管线：问题库模式 成功出队 / 失败保留 ────────────────────────────────────


def _seed_generation(app, *, fields):
    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool
    from server.app.modules.ai_generation.service import create_session
    from server.app.modules.prompt_templates.models import PromptTemplate
    from server.app.modules.skills.models import Skill

    uid = _admin_id(app.session_factory)
    skill_path = _seed_skill(app.data_dir)
    with app.session_factory() as db:
        skill = Skill(name="s", description="", storage_path=skill_path, is_enabled=True)
        db.add(skill)
        prompt = PromptTemplate(
            name="p", content="写一篇：{{问题}}", scope="generation", user_id=uid, is_enabled=True
        )
        db.add(prompt)
        pool = QuestionPool(user_id=uid, name="pool")
        db.add(pool)
        db.flush()
        item = QuestionItem(pool_id=pool.id, record_id="r1", fields=fields, status="pending")
        db.add(item)
        db.flush()
        session = create_session(
            db,
            user_id=uid,
            skill_id=skill.id,
            prompt_template_id=prompt.id,
            question_item_ids=[item.id],
        )
        db.commit()
        return session.id, item.id


def test_question_bank_pipeline_consumes_on_success(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation.models import GenerationSession, QuestionItem
        from server.app.modules.ai_generation.pipeline import run_pipeline

        session_id, item_id = _seed_generation(app, fields={"问题": "1.a、2.b、3.c"})
        monkeypatch.setattr(
            "litellm.completion", lambda **kw: _fake_completion("# 标题\n\n融合正文。")
        )

        with app.session_factory() as db:
            run_pipeline(db, session_id, session_factory=app.session_factory)

        with app.session_factory() as db:
            s = db.get(GenerationSession, session_id)
            it = db.get(QuestionItem, item_id)
            ids = json.loads(s.article_ids or "[]")
            assert s.status == "done"
            assert len(ids) == 1
            assert it.status == "consumed"
            assert it.article_id == ids[0]
    finally:
        app.cleanup()


def test_question_bank_pipeline_keeps_pending_on_failure(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation.models import GenerationSession, QuestionItem
        from server.app.modules.ai_generation.pipeline import run_pipeline

        session_id, item_id = _seed_generation(app, fields={"问题": "x"})

        def _boom(**kw):
            raise RuntimeError("LLM down")

        monkeypatch.setattr("litellm.completion", _boom)
        with app.session_factory() as db:
            run_pipeline(db, session_id, session_factory=app.session_factory)

        with app.session_factory() as db:
            s = db.get(GenerationSession, session_id)
            it = db.get(QuestionItem, item_id)
            assert s.status == "failed"
            assert it.status == "pending"  # 未出队，可重试
            assert it.article_id is None
    finally:
        app.cleanup()


# ── API ─────────────────────────────────────────────────────────────────────


def test_pool_create_sync_list_via_api(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        c = app.client
        r = c.post(
            "/api/generation/question-pools",
            json={"name": "P", "feishu_app_token": "app", "feishu_table_id": "tbl"},
        )
        assert r.status_code == 201, r.text
        pool_id = r.json()["id"]

        monkeypatch.setattr(
            "server.app.shared.feishu_bitable.list_bitable_records",
            lambda app_token, table_id: [{"record_id": "r1", "fields": {"问题": "q1"}}],
        )
        r = c.post(f"/api/generation/question-pools/{pool_id}/sync")
        assert r.status_code == 200, r.text
        assert r.json()["added"] == 1

        r = c.get(f"/api/generation/question-pools/{pool_id}/items")
        items = r.json()
        assert len(items) == 1 and items[0]["record_id"] == "r1"
    finally:
        app.cleanup()


def test_start_generation_accepts_question_items(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation.models import (
            GenerationSession,
            QuestionItem,
            QuestionPool,
        )
        from server.app.modules.prompt_templates.models import PromptTemplate
        from server.app.modules.skills.models import Skill

        uid = _admin_id(app.session_factory)
        with app.session_factory() as db:
            skill = Skill(name="s", description="", storage_path="x", is_enabled=True)
            db.add(skill)
            prompt = PromptTemplate(
                name="p", content="{{问题}}", scope="generation", user_id=uid, is_enabled=True
            )
            db.add(prompt)
            pool = QuestionPool(user_id=uid, name="pool")
            db.add(pool)
            db.flush()
            item = QuestionItem(
                pool_id=pool.id, record_id="r1", fields={"问题": "q"}, status="pending"
            )
            db.add(item)
            db.flush()
            db.commit()
            skill_id, prompt_id, item_id = skill.id, prompt.id, item.id

        r = app.client.post(
            "/api/generation/sessions",
            json={
                "skill_id": skill_id,
                "prompt_template_id": prompt_id,
                "question_item_ids": [item_id],
            },
        )
        assert r.status_code == 202, r.text
        session_id = r.json()["session_id"]
        with app.session_factory() as db:
            s = db.get(GenerationSession, session_id)
            assert json.loads(s.question_item_ids) == [item_id]
    finally:
        app.cleanup()


# ── 同步抽专用字段：提问词 + 分类板块 ──────────────────────────────────────


def test_sync_extracts_question_text_and_category(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation.models import QuestionItem, QuestionPool

        uid = _admin_id(app.session_factory)
        with app.session_factory() as db:
            pool = QuestionPool(user_id=uid, name="p", feishu_app_token="a", feishu_table_id="t")
            db.add(pool)
            db.flush()
            db.commit()
            pool_id = pool.id

        monkeypatch.setattr(
            "server.app.shared.feishu_bitable.list_bitable_records",
            lambda a, t: [
                {
                    "record_id": "r1",
                    "fields": {
                        "提问词": [{"type": "text", "text": "有没有无广告的游戏"}],
                        "分类板块": "无广告 / 不肝不氪",
                        "蒋纪缘": 2,
                    },
                },
                {
                    "record_id": "r2",
                    "fields": {
                        "提问词": "口碑好的游戏推荐",
                        "分类板块": "综合通用推荐",
                    },
                },
            ],
        )
        with app.session_factory() as db:
            qb.sync_pool(db, db.get(QuestionPool, pool_id))
            db.commit()
            items = {it.record_id: it for it in db.query(QuestionItem).filter_by(pool_id=pool_id)}
            assert items["r1"].question_text == "有没有无广告的游戏"
            assert items["r1"].category == "无广告 / 不肝不氪"
            assert items["r2"].question_text == "口碑好的游戏推荐"
            assert items["r2"].category == "综合通用推荐"
    finally:
        app.cleanup()


# ── 手动分组：按 category 合并 ───────────────────────────────────────────────


def test_group_items_by_category_preserves_first_seen_order():
    from server.app.modules.ai_generation.models import QuestionItem as QI

    items = [
        QI(id=1, pool_id=1, record_id="a", category="X", question_text="qa"),
        QI(id=2, pool_id=1, record_id="b", category="Y", question_text="qb"),
        QI(id=3, pool_id=1, record_id="c", category="X", question_text="qc"),
        QI(id=4, pool_id=1, record_id="d", category=None, question_text="qd"),
    ]
    groups = qb.group_items_by_category(items)
    assert [g[0] for g in groups] == ["X", "Y", None]
    assert [it.record_id for it in groups[0][1]] == ["a", "c"]


def test_format_question_group_numbers_questions():
    from server.app.modules.ai_generation.models import QuestionItem as QI

    items = [
        QI(question_text="无广告的游戏"),
        QI(question_text="不肝不氪"),
        QI(question_text="免费良心"),
    ]
    text = qb.format_question_group(items)
    assert text == "1. 无广告的游戏\n2. 不肝不氪\n3. 免费良心"


# ── 自动选题：板块优先级 + 轮转 + 随机 K + 不消费 ───────────────────────────


def _seed_multi_category_pool(app):
    """seed 一个池，3 个板块：A(2行) B(3行) C(1行)。返回 (pool_id, items by category)."""
    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool

    uid = _admin_id(app.session_factory)
    rows = [
        ("A", "a1"),
        ("A", "a2"),
        ("B", "b1"),
        ("B", "b2"),
        ("B", "b3"),
        ("C", "c1"),
    ]
    with app.session_factory() as db:
        pool = QuestionPool(user_id=uid, name="multi")
        db.add(pool)
        db.flush()
        for cat, q in rows:
            db.add(
                QuestionItem(
                    pool_id=pool.id,
                    record_id=q,
                    fields={},
                    question_text=q,
                    category=cat,
                    status="pending",
                )
            )
        db.commit()
        return pool.id


def test_list_categories_for_auto_unused_first_then_position(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation.models import CategoryUsage

        pool_id = _seed_multi_category_pool(app)
        # 给 A 一个 last_used_at（最近用过），B 也用过（更久前），C 从没上
        with app.session_factory() as db:
            now = datetime.utcnow()
            db.add(CategoryUsage(pool_id=pool_id, category="A", last_used_at=now))
            db.add(
                CategoryUsage(pool_id=pool_id, category="B", last_used_at=now - timedelta(days=1))
            )
            db.commit()

        with app.session_factory() as db:
            cats = qb.list_categories_for_auto(db, pool_id)
        # C 没用过排第一；B 比 A 早，排第二；A 最近用，排最后
        assert cats == ["C", "B", "A"]
    finally:
        app.cleanup()


def test_auto_pick_groups_round_robin_and_K_random(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        pool_id = _seed_multi_category_pool(
            app
        )  # A=2, B=3, C=1，全部 unused → 按 first_id 顺序 A B C
        rng = random.Random(42)  # 固定种子保证可复现

        with app.session_factory() as db:
            groups = qb.auto_pick_groups(db, pool_id, n=5, rng=rng)

        cats = [g[0] for g in groups]
        # 5 次轮转：A, B, C, A, B
        assert cats == ["A", "B", "C", "A", "B"]
        # 每组 K 在 [1, len(板块)] 区间
        size_map = {"A": 2, "B": 3, "C": 1}
        for cat, subset in groups:
            assert 1 <= len(subset) <= size_map[cat]
            # subset 全来自该板块
            assert all(it.category == cat for it in subset)
    finally:
        app.cleanup()


def test_mark_category_used_upserts_last_used_at(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation.models import CategoryUsage

        pool_id = _seed_multi_category_pool(app)
        with app.session_factory() as db:
            qb.mark_category_used(db, pool_id, "A")
            db.commit()
            usage = db.get(CategoryUsage, {"pool_id": pool_id, "category": "A"})
            assert usage is not None
            first_ts = usage.last_used_at

        with app.session_factory() as db:
            qb.mark_category_used(db, pool_id, "A")
            db.commit()
            usage = db.get(CategoryUsage, {"pool_id": pool_id, "category": "A"})
            assert usage.last_used_at >= first_ts
    finally:
        app.cleanup()


# ── 管线集成：手动多板块、自动模式 ───────────────────────────────────────


def _seed_session(app, *, pool_id, item_ids=None, auto_count=None):
    """造一个 session + 必需的 skill/prompt。返回 (session_id, skill_id, prompt_id)."""
    from server.app.modules.ai_generation.service import create_session
    from server.app.modules.prompt_templates.models import PromptTemplate
    from server.app.modules.skills.models import Skill

    uid = _admin_id(app.session_factory)
    skill_path = _seed_skill(app.data_dir)
    with app.session_factory() as db:
        skill = Skill(
            name=f"sk-{auto_count or 'm'}", description="", storage_path=skill_path, is_enabled=True
        )
        db.add(skill)
        prompt = PromptTemplate(
            name=f"pp-{auto_count or 'm'}",
            content="写一篇：{{问题}}",
            scope="generation",
            user_id=uid,
            is_enabled=True,
        )
        db.add(prompt)
        db.flush()
        s = create_session(
            db,
            user_id=uid,
            skill_id=skill.id,
            prompt_template_id=prompt.id,
            pool_id=pool_id,
            question_item_ids=item_ids or [],
            auto_count=auto_count,
        )
        db.commit()
        return s.id


def test_pipeline_manual_groups_by_category_into_one_article_per_category(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation.models import GenerationSession, QuestionItem
        from server.app.modules.ai_generation.pipeline import run_pipeline

        pool_id = _seed_multi_category_pool(app)
        # 勾选 A 的 2 条 + B 的 1 条 → 期望出 2 篇
        with app.session_factory() as db:
            a_ids = [
                it.id for it in db.query(QuestionItem).filter_by(pool_id=pool_id, category="A")
            ]
            b_ids = [
                it.id
                for it in db.query(QuestionItem).filter_by(pool_id=pool_id, category="B").limit(1)
            ]
        selected = a_ids + b_ids
        session_id = _seed_session(app, pool_id=pool_id, item_ids=selected)

        monkeypatch.setattr("litellm.completion", lambda **kw: _fake_completion("# 标题\n\n正文。"))
        with app.session_factory() as db:
            run_pipeline(db, session_id, session_factory=app.session_factory)

        with app.session_factory() as db:
            s = db.get(GenerationSession, session_id)
            assert s.status == "done"
            assert len(json.loads(s.article_ids)) == 2  # 2 个板块 → 2 篇
            # 选中的 items 全部 consumed
            for iid in selected:
                it = db.get(QuestionItem, iid)
                assert it.status == "consumed"
                assert it.article_id is not None
    finally:
        app.cleanup()


def test_pipeline_auto_picks_and_marks_category_without_consuming_items(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.ai_generation.models import (
            CategoryUsage,
            GenerationSession,
            QuestionItem,
        )
        from server.app.modules.ai_generation.pipeline import run_pipeline

        pool_id = _seed_multi_category_pool(app)  # A=2, B=3, C=1
        session_id = _seed_session(app, pool_id=pool_id, auto_count=2)

        monkeypatch.setattr("litellm.completion", lambda **kw: _fake_completion("# T\n\nx"))
        with app.session_factory() as db:
            run_pipeline(db, session_id, session_factory=app.session_factory)

        with app.session_factory() as db:
            s = db.get(GenerationSession, session_id)
            assert s.status == "done"
            assert len(json.loads(s.article_ids)) == 2
            # 自动模式：item.status 都还 pending（不消费）
            pending = db.query(QuestionItem).filter_by(pool_id=pool_id, status="pending").count()
            assert pending == 6
            # CategoryUsage 至少有 2 条（按轮转 A、B 被用）
            usages = db.query(CategoryUsage).filter_by(pool_id=pool_id).all()
            assert {u.category for u in usages} == {"A", "B"}
    finally:
        app.cleanup()
