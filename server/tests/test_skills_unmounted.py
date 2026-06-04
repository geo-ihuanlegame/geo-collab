"""Skill 管理已下线：/api/skills 不再挂载。"""

from __future__ import annotations

import pytest

from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_skills_endpoint_unmounted(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        resp = client.get("/api/skills")
        assert resp.status_code == 404
    finally:
        test_app.cleanup()
