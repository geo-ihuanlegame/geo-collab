"""公开 / 受保护端点边界契约。

钉住 CLAUDE.md 的有意设计：`/api/stock-images/*` 是公开的图片文件服务（文章正文嵌图后
需公开可访问），而 `/api/image-library/*` 等业务端点必须登录。这组测试防止两类回归：
  1) 未来「一刀切加鉴权」误伤公开图片服务，导致已发布文章里的图片 401；
  2) 反过来，公开端点意外被要求登录，或受保护端点的鉴权被整体打挂。
"""

import pytest
from fastapi.testclient import TestClient

from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_stock_images_file_is_public(monkeypatch):
    """/api/stock-images/{id}/file 无 cookie 也不应 401（公开）。

    用一个不存在的 id：若端点被鉴权保护，会在进入 handler 前 401；公开端点则进入 handler
    后因图片不存在返回 404。所以「404 而非 401」即证明它是公开的。
    """
    test_app = build_test_app(monkeypatch)
    try:
        anon = TestClient(test_app.client.app)  # 全新客户端，不带 access_token cookie
        resp = anon.get("/api/stock-images/999999/file")
        assert resp.status_code != 401, "公开图片文件服务不应要求登录"
        assert resp.status_code == 404, resp.text  # 已进入 handler、图片不存在 → 404
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_image_library_requires_auth(monkeypatch):
    """/api/image-library/* 无 cookie 必须 401；带 admin cookie 时仍正常 200。"""
    test_app = build_test_app(monkeypatch)
    try:
        anon = TestClient(test_app.client.app)
        resp = anon.get("/api/image-library/categories")
        assert resp.status_code == 401, resp.text

        # 反向 sanity：带 admin cookie 时该端点正常工作（确认不是被整体打挂才「碰巧」401）
        ok = test_app.client.get("/api/image-library/categories")
        assert ok.status_code == 200, ok.text
    finally:
        test_app.cleanup()
