"""AI 模型注册表解析器：resolve_writing_engine / resolve_format_engine 优先级。

DB 行优先（默认行 / selected 命中 / disabled 忽略）→ api_key_env env → scope 全局 key →
写作回落 config.resolve_engine、格式回落 settings.ai_format_*。
"""

from __future__ import annotations

import pytest

from server.app.core.config import get_settings
from server.app.modules.ai_models.models import AiModel
from server.app.modules.ai_models.service import resolve_format_engine, resolve_writing_engine
from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


def _add(db, **kw) -> AiModel:
    defaults = dict(
        label="L",
        model="",
        scope="generation",
        base_url=None,
        api_key_env=None,
        is_enabled=True,
        is_default=False,
        sort_order=0,
    )
    defaults.update(kw)
    defaults["is_default_key"] = defaults["scope"] if defaults["is_default"] else None
    if defaults["is_default"]:
        # 清同 scope 既有默认（含 create_app 播种的），免撞 uq_ai_models_scope_default
        db.query(AiModel).filter(
            AiModel.scope == defaults["scope"], AiModel.is_default.is_(True)
        ).update(
            {AiModel.is_default: False, AiModel.is_default_key: None},
            synchronize_session=False,
        )
    row = AiModel(**defaults)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_writing_default_row_wins(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        with app.session_factory() as db:
            _add(db, model="claude-opus-4-8", base_url="https://relay/v1", is_default=True)
            _add(db, model="claude-sonnet-4-6")
            model, _key, base = resolve_writing_engine(db, None)
        assert model == "claude-opus-4-8"
        assert base == "https://relay/v1"
    finally:
        app.cleanup()


def test_writing_selected_matches_enabled_row(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        with app.session_factory() as db:
            _add(db, model="claude-opus-4-8", is_default=True)
            _add(db, model="anthropic/claude-haiku-4-5", base_url="http://relay:8080/api")
            model, _key, base = resolve_writing_engine(db, "anthropic/claude-haiku-4-5")
        assert model == "anthropic/claude-haiku-4-5"
        assert base == "http://relay:8080/api"
    finally:
        app.cleanup()


def test_writing_disabled_row_falls_back(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        with app.session_factory() as db:
            _add(db, model="disabled-model", is_enabled=False)
            model, _key, base = resolve_writing_engine(db, "disabled-model")
        # 无 enabled 行命中 → 委托 config.resolve_engine：原样用 model 串 + 默认 key、base=None
        assert model == "disabled-model"
        assert base is None
    finally:
        app.cleanup()


def test_writing_no_row_uses_config_default(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        with app.session_factory() as db:
            model, _key, base = resolve_writing_engine(db, None)
        # 空 DB（仅 seed 的 model="" 默认行）+ None → 落 settings.ai_model 默认串
        assert model == get_settings().ai_model
        assert base is None
    finally:
        app.cleanup()


def test_writing_api_key_env_then_global(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_AI_API_KEY", "global-key")
        monkeypatch.setenv("MY_RELAY_KEY", "relay-key")
        get_settings.cache_clear()
        with app.session_factory() as db:
            _add(db, model="m-env", api_key_env="MY_RELAY_KEY")
            _add(db, model="m-global", api_key_env=None)
            _, k_env, _ = resolve_writing_engine(db, "m-env")
            _, k_global, _ = resolve_writing_engine(db, "m-global")
            # api_key_env 设了但 env 缺 → 回落全局
            _add(db, model="m-missing", api_key_env="NOT_SET_VAR")
            _, k_missing, _ = resolve_writing_engine(db, "m-missing")
        assert k_env == "relay-key"
        assert k_global == "global-key"
        assert k_missing == "global-key"
    finally:
        app.cleanup()


def test_format_default_row(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_AI_FORMAT_API_KEY", "fmt-key")
        get_settings.cache_clear()
        with app.session_factory() as db:
            _add(
                db,
                model="deepseek/deepseek-v4-pro",
                scope="ai_format",
                base_url="https://relay/v1",
                is_default=True,
            )
            model, key, base, timeout = resolve_format_engine(db, None)
        assert model == "deepseek/deepseek-v4-pro"
        assert base == "https://relay/v1"
        assert key == "fmt-key"
        assert timeout == get_settings().ai_format_timeout_seconds
    finally:
        app.cleanup()


def test_format_no_row_falls_back_to_settings(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_AI_FORMAT_API_KEY", "fmt-key")
        get_settings.cache_clear()
        with app.session_factory() as db:
            # 删掉 seed 的 ai_format 默认行 → 无行
            db.query(AiModel).filter(AiModel.scope == "ai_format").delete()
            db.commit()
            model, key, base, _timeout = resolve_format_engine(db, None)
        assert model == get_settings().ai_format_model
        assert base is None
        assert key == "fmt-key"
    finally:
        app.cleanup()
