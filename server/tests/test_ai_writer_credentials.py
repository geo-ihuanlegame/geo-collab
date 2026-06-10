"""写作内核 generate_article_from_prompt 把「选中引擎的」model/api_key/api_base
正确传给 litellm。LiteLLM mock，不真实出网。需 MySQL（落 create_article）。
"""

import json
from types import SimpleNamespace

from server.tests.utils import build_test_app


def _fake_completion(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _admin_id(session_factory) -> int:
    from server.app.modules.system.models import User

    with session_factory() as db:
        return db.query(User).first().id


def _run_writer(app, monkeypatch, *, selected_model):
    from server.app.core.config import get_settings
    from server.app.modules.ai_generation.article_writer import generate_article_from_prompt

    seen: dict = {}

    def _cap(**kw):
        seen.update(model=kw.get("model"), api_key=kw.get("api_key"), api_base=kw.get("api_base"))
        return _fake_completion("# 标题\n\n正文")

    monkeypatch.setattr("litellm.completion", _cap)
    get_settings.cache_clear()
    uid = _admin_id(app.session_factory)
    generate_article_from_prompt(
        session_factory=app.session_factory,
        user_id=uid,
        template_content="写：{{问题}}",
        question_text="1. 问题a1",
        model=selected_model,
    )
    return seen


def test_writer_uses_selected_engine_key_and_base_url(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_AI_MODEL", "default-model")
        monkeypatch.setenv("GEO_AI_API_KEY", "default-key")
        monkeypatch.setenv(
            "GEO_AI_ENGINES",
            json.dumps(
                [
                    {
                        "label": "网关",
                        "model": "openai/gpt-4o",
                        "api_key": "gw-key",
                        "base_url": "https://gw/v1",
                    }
                ]
            ),
        )
        seen = _run_writer(app, monkeypatch, selected_model="openai/gpt-4o")
        assert seen["model"] == "openai/gpt-4o"
        assert seen["api_key"] == "gw-key"
        assert seen["api_base"] == "https://gw/v1"
    finally:
        from server.app.core.config import get_settings

        get_settings.cache_clear()
        app.cleanup()


def test_writer_default_engine_uses_default_key_no_base_url(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_AI_MODEL", "default-model")
        monkeypatch.setenv("GEO_AI_API_KEY", "default-key")
        monkeypatch.setenv("GEO_AI_ENGINES", json.dumps([{"label": "默认", "model": ""}]))
        seen = _run_writer(app, monkeypatch, selected_model=None)
        assert seen["model"] == "default-model"
        assert seen["api_key"] == "default-key"
        assert seen["api_base"] is None
    finally:
        from server.app.core.config import get_settings

        get_settings.cache_clear()
        app.cleanup()
