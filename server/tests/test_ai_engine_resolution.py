"""resolve_engine 纯函数分支：默认引擎 / 命中带凭据 / 引擎 key 空回落 / 列表外原样。

不依赖 DB，裸 pytest 即可跑。每个用例用 monkeypatch.setenv + cache_clear 隔离配置。
"""

import json

from server.app.core.config import get_settings, resolve_engine


def _set_engines(monkeypatch, engines: list[dict]) -> None:
    monkeypatch.setenv("GEO_AI_MODEL", "default-model")
    monkeypatch.setenv("GEO_AI_API_KEY", "default-key")
    monkeypatch.setenv("GEO_AI_ENGINES", json.dumps(engines))
    get_settings.cache_clear()


def test_resolve_engine_empty_returns_default(monkeypatch):
    _set_engines(monkeypatch, [{"label": "x", "model": "m", "api_key": "k"}])
    try:
        assert resolve_engine("") == ("default-model", "default-key", None)
        assert resolve_engine(None) == ("default-model", "default-key", None)
        assert resolve_engine("   ") == ("default-model", "default-key", None)
    finally:
        get_settings.cache_clear()


def test_resolve_engine_hit_uses_own_credentials(monkeypatch):
    _set_engines(
        monkeypatch,
        [
            {
                "label": "DS",
                "model": "deepseek/deepseek-chat",
                "api_key": "ds-key",
                "base_url": "https://ds/v1",
            }
        ],
    )
    try:
        assert resolve_engine("deepseek/deepseek-chat") == (
            "deepseek/deepseek-chat",
            "ds-key",
            "https://ds/v1",
        )
    finally:
        get_settings.cache_clear()


def test_resolve_engine_blank_key_falls_back_to_default_key(monkeypatch):
    _set_engines(monkeypatch, [{"label": "C2", "model": "claude-x", "api_key": ""}])
    try:
        assert resolve_engine("claude-x") == ("claude-x", "default-key", None)
    finally:
        get_settings.cache_clear()


def test_resolve_engine_unknown_model_passthrough_with_default_key(monkeypatch):
    _set_engines(monkeypatch, [{"label": "DS", "model": "deepseek/deepseek-chat", "api_key": "ds"}])
    try:
        assert resolve_engine("gpt-foo") == ("gpt-foo", "default-key", None)
    finally:
        get_settings.cache_clear()


def test_ai_engines_parse_credentials_from_json(monkeypatch):
    _set_engines(
        monkeypatch,
        [{"label": "网关", "model": "openai/gpt-4o", "api_key": "gw", "base_url": "https://gw/v1"}],
    )
    try:
        e = get_settings().ai_engines[0]
        assert (e.label, e.model, e.api_key, e.base_url) == (
            "网关",
            "openai/gpt-4o",
            "gw",
            "https://gw/v1",
        )
    finally:
        get_settings.cache_clear()
