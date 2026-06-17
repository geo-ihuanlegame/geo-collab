"""Task 1c —— POST /api/system/refresh-settings：admin 手动刷新配置缓存。

替代原先 ai_format/baidu 每次调用都 get_settings.cache_clear() 的做法（破坏 lru_cache 契约 +
每调用重建 Settings）。改为显式端点：运维改环境变量 / .env 后调一次即生效。
前提：web 单进程（现状 uvicorn 无 --workers）；多进程下只刷到接到请求的那个进程。
"""

from __future__ import annotations

import pytest

from server.tests.utils import build_test_app, create_extra_user


@pytest.mark.mysql
def test_refresh_settings_requires_admin(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _uid, op_client = create_extra_user(test_app, "op-refresh", role="operator")
        r = op_client.post("/api/system/refresh-settings")
        assert r.status_code == 403
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_refresh_settings_clears_get_settings_cache(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.core.config import get_settings

        # 填充缓存
        before = get_settings().ai_generate_max_count
        # 改环境但不刷新 → 仍读到缓存旧值（证明确实缓存、没有每次重读）
        monkeypatch.setenv("GEO_AI_GENERATE_MAX_COUNT", str(before + 7))
        assert get_settings().ai_generate_max_count == before
        # admin 调刷新端点 → lru_cache 清空，下次读取重建
        r = test_app.client.post("/api/system/refresh-settings")
        assert r.status_code == 200
        assert get_settings().ai_generate_max_count == before + 7
    finally:
        test_app.cleanup()
