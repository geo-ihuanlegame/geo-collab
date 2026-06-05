import pytest

from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_ai_generate_rejects_other_users_template(monkeypatch):
    from server.app.modules.pipelines.nodes.ai_generate_node import run_ai_generate
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.prompt_templates.models import PromptTemplate
    from server.app.modules.system.models import User
    from server.app.shared.errors import ValidationError

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            owner = db.query(User).first()
            other = User(
                username="other",
                role="operator",
                is_active=True,
                must_change_password=False,
            )
            other.set_password("password1")
            db.add(other)
            db.flush()
            tpl = PromptTemplate(
                user_id=owner.id,
                name="私有",
                content="写：",
                scope="generation",
                is_enabled=True,
            )
            db.add(tpl)
            db.commit()
            tpl_id, other_id = tpl.id, other.id

        ctx = NodeRunContext(
            session_factory=test_app.session_factory,
            user_id=other_id,
            config={"prompt_template_id": tpl_id, "count": 1},
            inputs={"question_text": "主题"},
            upstream={},
        )
        with pytest.raises(ValidationError):
            run_ai_generate(ctx)
    finally:
        test_app.cleanup()
