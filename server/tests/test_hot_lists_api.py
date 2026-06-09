import pytest
from fastapi.testclient import TestClient

from server.app.modules.hot_lists import service
from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


def test_get_source_passthrough(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        async def fake_fetch_source(source, *, limit, no_cache, client=None):
            return 200, {"code": 200, "name": source, "data": [{"id": "1", "title": "x", "url": "u"}]}

        monkeypatch.setattr(
            "server.app.modules.hot_lists.service.fetch_source", fake_fetch_source
        )
        resp = test_app.client.get("/api/hot-lists/weibo")
        assert resp.status_code == 200
        assert resp.json()["name"] == "weibo"
    finally:
        test_app.cleanup()


def test_upstream_down_returns_502(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        async def boom(source, *, limit, no_cache, client=None):
            raise service.HotListUpstreamError("down")

        monkeypatch.setattr("server.app.modules.hot_lists.service.fetch_source", boom)
        resp = test_app.client.get("/api/hot-lists/weibo")
        assert resp.status_code == 502
    finally:
        test_app.cleanup()


def test_invalid_source_rejected(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        # 大写不匹配 ^[a-z0-9-]+$ → 400（不打上游）
        resp = test_app.client.get("/api/hot-lists/WEIBO")
        assert resp.status_code == 400
    finally:
        test_app.cleanup()


def test_requires_auth(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        noauth = TestClient(test_app.client.app)  # 不带 access_token cookie
        resp = noauth.get("/api/hot-lists/weibo")
        assert resp.status_code == 401
    finally:
        test_app.cleanup()
