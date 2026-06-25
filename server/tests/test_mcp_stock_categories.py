"""GET /api/mcp/stock-categories MCP endpoint 测试.

5 用例覆盖：鉴权 / 不带 kind 返全量 / kind 过滤 / image_count 正确性 / 排序.
"""

from __future__ import annotations

import pytest

from server.tests.utils import build_test_app


def _mk_category(
    test_app,
    *,
    name: str,
    kind: str = "main",
    description: str | None = None,
    official_url: str | None = None,
) -> int:
    """Helper: 建一条 StockCategory，返 id."""
    from server.app.modules.image_library.models import StockCategory

    db = test_app.session_factory()
    try:
        cat = StockCategory(
            name=name,
            bucket_name=f"bucket-{name}".lower().replace(" ", "-"),
            kind=kind,
            description=description,
            official_url=official_url,
        )
        db.add(cat)
        db.commit()
        return cat.id
    finally:
        db.close()


def _mk_image(test_app, *, category_id: int, filename: str = "img.jpg") -> int:
    """Helper: 建一条 StockImage 挂某栏目下，返 id."""
    from server.app.modules.image_library.models import StockImage

    db = test_app.session_factory()
    try:
        img = StockImage(
            category_id=category_id,
            minio_key=f"key-{filename}-{category_id}",
            filename=filename,
        )
        db.add(img)
        db.commit()
        return img.id
    finally:
        db.close()


@pytest.mark.mysql
def test_endpoint_requires_mcp_token(monkeypatch):
    """不带 X-MCP-Token → 401."""
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        r = test_app.client.get("/api/mcp/stock-categories")
        assert r.status_code == 401
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_endpoint_returns_all_categories_when_no_kind_filter(monkeypatch):
    """seed 2 main + 1 companion，不带 kind → 返 3 条；字段齐."""
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        _mk_category(
            test_app,
            name="餐厅养成记",
            kind="main",
            description="餐厅经营",
            official_url="https://example.com/restaurant",
        )
        _mk_category(test_app, name="江南百景图", kind="main")
        _mk_category(test_app, name="陪衬通用", kind="companion")

        r = test_app.client.get(
            "/api/mcp/stock-categories",
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body, list)
        assert len(body) == 3
        # 字段齐
        for item in body:
            assert set(item.keys()) >= {
                "id",
                "name",
                "kind",
                "description",
                "official_url",
                "image_count",
            }
        # 找餐厅养成记验非空字段
        rest = next(c for c in body if c["name"] == "餐厅养成记")
        assert rest["kind"] == "main"
        assert rest["description"] == "餐厅经营"
        assert rest["official_url"] == "https://example.com/restaurant"
        assert rest["image_count"] == 0  # 没 seed 图
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_endpoint_filters_by_kind_main(monkeypatch):
    """同 seed，?kind=main → 返 2 条 main，无 companion."""
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        _mk_category(test_app, name="餐厅养成记", kind="main")
        _mk_category(test_app, name="江南百景图", kind="main")
        _mk_category(test_app, name="陪衬通用", kind="companion")

        r = test_app.client.get(
            "/api/mcp/stock-categories",
            params={"kind": "main"},
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 2
        assert all(c["kind"] == "main" for c in body)
        assert {"餐厅养成记", "江南百景图"} == {c["name"] for c in body}
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_endpoint_image_count_correct(monkeypatch):
    """给某栏目 seed 3 个 StockImage → image_count=3；空栏目 image_count=0."""
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        full = _mk_category(test_app, name="有图栏目", kind="main")
        empty = _mk_category(test_app, name="空栏目", kind="main")
        for i in range(3):
            _mk_image(test_app, category_id=full, filename=f"a{i}.jpg")

        r = test_app.client.get(
            "/api/mcp/stock-categories",
            headers={"X-MCP-Token": "secret"},
        )
        body = r.json()
        full_item = next(c for c in body if c["id"] == full)
        empty_item = next(c for c in body if c["id"] == empty)
        assert full_item["image_count"] == 3
        assert empty_item["image_count"] == 0
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_endpoint_order_main_before_companion(monkeypatch):
    """seed mixed kind 顺序混乱，返回顺序 main 在 companion 之前（kind asc）."""
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        # 故意先建 companion 再 main，验排序按 kind 不按 id
        _mk_category(test_app, name="陪衬A", kind="companion")
        _mk_category(test_app, name="主推B", kind="main")
        _mk_category(test_app, name="陪衬C", kind="companion")
        _mk_category(test_app, name="主推D", kind="main")

        r = test_app.client.get(
            "/api/mcp/stock-categories",
            headers={"X-MCP-Token": "secret"},
        )
        body = r.json()
        kinds = [c["kind"] for c in body]
        # main 优先（CASE priority 0），companion 在后；所以应该是 [main, main, companion, companion]
        assert kinds == ["main", "main", "companion", "companion"]
    finally:
        test_app.cleanup()
