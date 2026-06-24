"""TDD: AI 生文落库时 review_status 必须是 'pending'。

Task 1 (2026-06-05-pipelines-vibecoding-remediation.md):
堵审核绕过根因 — generate_article_from_prompt 在 create_article 之后、commit 之前
显式设 article.review_status = "pending"，不依赖 run 后 mark_pending_and_group 翻转。
"""

import pytest

from server.tests.utils import build_test_app


def _fake_completion(text: str):
    class _Msg:
        content = text

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    return _Resp()


@pytest.mark.mysql
def test_generated_article_is_born_pending(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        monkeypatch.setattr(
            "litellm.completion", lambda **kw: _fake_completion("# 标题\n\n正文段落。")
        )

        from server.app.modules.ai_generation.article_writer import generate_article_from_prompt
        from server.app.modules.articles.models import Article
        from server.app.modules.system.models import User

        # 取 admin 用户 id（testadmin 由 build_test_app 种入）
        with app.session_factory() as db:
            admin_id = db.query(User).filter(User.username == "testadmin").first().id

        article_id = generate_article_from_prompt(
            session_factory=app.session_factory,
            user_id=admin_id,
            template_content="写一篇文章：{{问题}}",
            question_text="测试问题",
            model=None,
        )

        with app.session_factory() as db:
            art = db.get(Article, article_id)
            assert art is not None, "文章应已落库"
            assert art.review_status == "pending", (
                f"AI 生文必须 review_status='pending'，实际为 '{art.review_status}'"
            )
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_generated_article_records_source_provenance(monkeypatch):
    """生文溯源：传入的「智能体名 / 模板名」必须落库到 Article，缺省时为 None。"""
    app = build_test_app(monkeypatch)
    try:
        monkeypatch.setattr(
            "litellm.completion", lambda **kw: _fake_completion("# 标题\n\n正文段落。")
        )

        from server.app.modules.ai_generation.article_writer import generate_article_from_prompt
        from server.app.modules.articles.models import Article
        from server.app.modules.system.models import User

        with app.session_factory() as db:
            admin_id = db.query(User).filter(User.username == "testadmin").first().id

        # 带溯源 → 两列写入对应名字
        stamped_id = generate_article_from_prompt(
            session_factory=app.session_factory,
            user_id=admin_id,
            template_content="写：{{问题}}",
            question_text="q",
            model=None,
            source_agent_name="生文自动",
            source_template_name="游戏榜单清单",
        )
        # 不带溯源（手动/旧路径） → 两列保持 None
        bare_id = generate_article_from_prompt(
            session_factory=app.session_factory,
            user_id=admin_id,
            template_content="写：{{问题}}",
            question_text="q",
            model=None,
        )

        with app.session_factory() as db:
            stamped = db.get(Article, stamped_id)
            assert stamped.source_agent_name == "生文自动"
            assert stamped.source_template_name == "游戏榜单清单"
            bare = db.get(Article, bare_id)
            assert bare.source_agent_name is None
            assert bare.source_template_name is None
    finally:
        app.cleanup()
