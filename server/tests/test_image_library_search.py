"""图片库全库搜索 + 栏目 latest_image_at 测试。

TDD 先写测试，按 §测试 清单覆盖：
- 多字段命中（filename / description / 标签 / 栏目名各一例）
- 跨栏目：两个栏目各放命中图，一次 q 都能搜到
- 标签模糊：json_search 命中数组里的某个标签子串
- limit clamp（>200 截到 200、缺省 50）
- limit=0 → 422（ge=1 边界）
- 空 q 返回 []
- LIKE 转义：含 % / _ 的 q 按字面匹配，不当通配符
- latest_image_at：有图栏目返回最新图 created_at、无图栏目返回 None
"""

from datetime import UTC, datetime, timedelta

import pytest

from server.app.modules.image_library.models import StockCategory, StockImage
from server.tests.utils import build_test_app


def _patch_minio(monkeypatch):
    """测试环境无 MinIO：建桶/删桶/上传全部打成 no-op。"""
    monkeypatch.setattr(
        "server.app.modules.image_library.router.minio_store.ensure_bucket",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "server.app.modules.image_library.router.minio_store.remove_bucket",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "server.app.modules.image_library.router.minio_store.upload_image",
        lambda *a, **k: None,
    )


def _insert_category(db, name, bucket, kind="companion"):
    cat = StockCategory(name=name, bucket_name=bucket, kind=kind)
    db.add(cat)
    db.flush()
    return cat


def _insert_image(db, category_id, filename, *, description=None, tags=None, created_at=None):
    img = StockImage(
        category_id=category_id,
        minio_key=f"test-key-{filename}",
        filename=filename,
        description=description,
        tags=tags or [],
    )
    if created_at is not None:
        img.created_at = created_at
    db.add(img)
    db.flush()
    return img


# ── 多字段命中 ─────────────────────────────────────────────────────────────


@pytest.mark.mysql
def test_search_hit_by_filename(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            cat = _insert_category(db, "测试栏目", "test-bucket", "companion")
            _insert_image(db, cat.id, "sunflower_photo.jpg")
            _insert_image(db, cat.id, "rose.png")
            db.commit()
            cat_id = cat.id

        r = app.client.get("/api/image-library/search?q=sunflower")
        assert r.status_code == 200, r.text
        results = r.json()
        assert len(results) == 1
        assert results[0]["filename"] == "sunflower_photo.jpg"
        assert results[0]["category_id"] == cat_id
        assert results[0]["category_name"] == "测试栏目"
        assert results[0]["kind"] == "companion"
        assert results[0]["url"].startswith("/api/stock-images/")
        assert results[0]["url"].endswith("/file")
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_search_hit_by_description(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            cat = _insert_category(db, "描述测试", "desc-bucket", "main")
            _insert_image(db, cat.id, "img1.jpg", description="这是一朵玫瑰花的特写")
            _insert_image(db, cat.id, "img2.jpg", description="普通风景图")
            db.commit()

        r = app.client.get("/api/image-library/search?q=玫瑰花")
        assert r.status_code == 200, r.text
        results = r.json()
        assert len(results) == 1
        assert results[0]["filename"] == "img1.jpg"
        assert results[0]["kind"] == "main"
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_search_hit_by_tag(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            cat = _insert_category(db, "标签测试", "tag-bucket", "companion")
            _insert_image(db, cat.id, "tagged.jpg", tags=["nature", "landscape", "mountain"])
            _insert_image(db, cat.id, "no-tag.jpg", tags=["city"])
            db.commit()

        r = app.client.get("/api/image-library/search?q=landscape")
        assert r.status_code == 200, r.text
        results = r.json()
        assert len(results) == 1
        assert results[0]["filename"] == "tagged.jpg"
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_search_hit_by_category_name(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            cat_match = _insert_category(db, "春季花卉", "spring-flowers", "companion")
            cat_other = _insert_category(db, "秋季风景", "autumn-scenes", "companion")
            _insert_image(db, cat_match.id, "flower1.jpg")
            _insert_image(db, cat_other.id, "autumn1.jpg")
            db.commit()

        r = app.client.get("/api/image-library/search?q=春季")
        assert r.status_code == 200, r.text
        results = r.json()
        assert len(results) == 1
        assert results[0]["category_name"] == "春季花卉"
        assert results[0]["filename"] == "flower1.jpg"
    finally:
        app.cleanup()


# ── 跨栏目搜索 ────────────────────────────────────────────────────────────


@pytest.mark.mysql
def test_search_cross_category(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            cat1 = _insert_category(db, "栏目A", "cat-a", "main")
            cat2 = _insert_category(db, "栏目B", "cat-b", "companion")
            _insert_image(db, cat1.id, "ocean_view.jpg")
            _insert_image(db, cat2.id, "ocean_sunset.jpg")
            _insert_image(db, cat1.id, "mountain.jpg")
            db.commit()

        r = app.client.get("/api/image-library/search?q=ocean")
        assert r.status_code == 200, r.text
        results = r.json()
        assert len(results) == 2
        filenames = {item["filename"] for item in results}
        assert filenames == {"ocean_view.jpg", "ocean_sunset.jpg"}
        # 两个不同 category
        cat_ids = {item["category_id"] for item in results}
        assert len(cat_ids) == 2
    finally:
        app.cleanup()


# ── 标签模糊子串匹配 ──────────────────────────────────────────────────────


@pytest.mark.mysql
def test_search_tag_fuzzy_substring(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            cat = _insert_category(db, "模糊栏目", "fuzzy-bucket", "companion")
            # 标签里有 "waterfall_scene"，搜索 "waterfall" 应命中
            _insert_image(db, cat.id, "img.jpg", tags=["waterfall_scene", "nature"])
            _insert_image(db, cat.id, "other.jpg", tags=["forest"])
            db.commit()

        r = app.client.get("/api/image-library/search?q=waterfall")
        assert r.status_code == 200, r.text
        results = r.json()
        assert len(results) == 1
        assert results[0]["filename"] == "img.jpg"
    finally:
        app.cleanup()


# ── limit clamp ──────────────────────────────────────────────────────────


@pytest.mark.mysql
def test_search_default_limit_50(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            cat = _insert_category(db, "大栏目", "big-bucket", "companion")
            # 插 60 张图，文件名都含 "batch"
            for i in range(60):
                _insert_image(db, cat.id, f"batch_image_{i:03d}.jpg")
            db.commit()

        r = app.client.get("/api/image-library/search?q=batch")
        assert r.status_code == 200, r.text
        results = r.json()
        # 默认 limit=50，超出被截断
        assert len(results) == 50
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_search_limit_clamp_over_200(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            cat = _insert_category(db, "限制栏目", "clamp-bucket", "companion")
            for i in range(250):
                _insert_image(db, cat.id, f"clamp_img_{i:03d}.jpg")
            db.commit()

        # limit=300 超过 200，应被 clamp 到 200
        r = app.client.get("/api/image-library/search?q=clamp&limit=300")
        assert r.status_code == 200, r.text
        results = r.json()
        assert len(results) == 200
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_search_explicit_small_limit(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            cat = _insert_category(db, "小限制栏目", "small-limit-bucket", "companion")
            for i in range(10):
                _insert_image(db, cat.id, f"small_img_{i}.jpg")
            db.commit()

        r = app.client.get("/api/image-library/search?q=small&limit=3")
        assert r.status_code == 200, r.text
        results = r.json()
        assert len(results) == 3
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_search_limit_zero_returns_422(monkeypatch):
    """limit=0 违反 ge=1 约束，FastAPI 应返回 422。"""
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        r = app.client.get("/api/image-library/search?q=anything&limit=0")
        assert r.status_code == 422, f"预期 422，实际 {r.status_code}: {r.text}"
    finally:
        app.cleanup()


# ── 空 q 返回 [] ──────────────────────────────────────────────────────────


@pytest.mark.mysql
def test_search_empty_q_returns_empty(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            cat = _insert_category(db, "空搜索栏目", "empty-q-bucket", "companion")
            _insert_image(db, cat.id, "some_image.jpg")
            db.commit()

        # 空字符串
        r = app.client.get("/api/image-library/search?q=")
        assert r.status_code == 200, r.text
        assert r.json() == []
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_search_whitespace_only_q_returns_empty(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            cat = _insert_category(db, "空白搜索栏目", "whitespace-bucket", "companion")
            _insert_image(db, cat.id, "img.jpg")
            db.commit()

        # 纯空白字符（strip 后为空）
        r = app.client.get("/api/image-library/search?q=   ")
        assert r.status_code == 200, r.text
        assert r.json() == []
    finally:
        app.cleanup()


# ── LIKE 转义 ────────────────────────────────────────────────────────────


@pytest.mark.mysql
def test_search_like_escape_percent(monkeypatch):
    """q 中的 % 应按字面匹配，不当通配符。

    distractor 说明：
    - "sale_50%_off.jpg" —— 含字面 "50%"，应命中
    - "img_50abc.jpg"    —— 含 "50" 后跟其它字符；若 % 未转义，
                           LIKE '%50%%' 会匹配它；若转义正确则不命中
    """
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            cat = _insert_category(db, "转义栏目", "escape-bucket", "companion")
            # 含字面 "50%" 的文件名 —— 预期命中
            _insert_image(db, cat.id, "sale_50%_off.jpg")
            # distractor：含 "50" 后跟非 % 字符；未转义的 %50%% 会过匹配它
            _insert_image(db, cat.id, "img_50abc.jpg")
            db.commit()

        # 搜 "50%" —— 若未转义，% 会当通配符，distractor 也会命中
        r = app.client.get("/api/image-library/search?q=50%25")
        assert r.status_code == 200, r.text
        results = r.json()
        assert len(results) == 1, f"预期仅1条（字面匹配），实际: {[x['filename'] for x in results]}"
        assert results[0]["filename"] == "sale_50%_off.jpg"
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_search_like_escape_underscore(monkeypatch):
    """q 中的 _ 应按字面匹配，不当通配符。

    distractor 说明：
    - "img_x_test.jpg" —— 含字面 "_x_"，应命中
    - "imgXtest.jpg"   —— 含 "gXt"（大写 X）；MySQL 默认 ci 排序规则下，
                          若 _ 未转义，LIKE '%_x_%' 把 _ 当单字符通配符，
                          "gXt" 满足 _x_ 模式（g→_, X→x ci, t→_），会被命中；
                          转义后 _ 是字面下划线，"imgXtest" 无字面 "_x_"，不命中
    """
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            cat = _insert_category(db, "下划线栏目", "underscore-bucket", "companion")
            # 含字面 "_x_" 的文件名 —— 预期命中
            _insert_image(db, cat.id, "img_x_test.jpg")
            # distractor：未转义的 _x_ 会把 gXt 当 _x_（ci），转义后不命中
            _insert_image(db, cat.id, "imgXtest.jpg")
            db.commit()

        # 搜 "_x_" —— 若未转义，_ 匹配任意单字符，distractor 也会命中
        r = app.client.get("/api/image-library/search?q=_x_")
        assert r.status_code == 200, r.text
        results = r.json()
        # 只有字面含 "_x_" 的那张应命中
        assert len(results) == 1, f"预期仅1条（字面匹配），实际: {[x['filename'] for x in results]}"
        assert results[0]["filename"] == "img_x_test.jpg"
    finally:
        app.cleanup()


# ── 返回字段结构 ──────────────────────────────────────────────────────────


@pytest.mark.mysql
def test_search_response_shape(monkeypatch):
    """验证返回的每项都有必要字段且 url 格式正确。"""
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            cat = _insert_category(db, "结构测试栏目", "shape-bucket", "main")
            img = _insert_image(db, cat.id, "shape_test.jpg")
            db.commit()
            img_id = img.id
            cat_id = cat.id

        r = app.client.get("/api/image-library/search?q=shape")
        assert r.status_code == 200, r.text
        results = r.json()
        assert len(results) == 1
        item = results[0]
        assert "id" in item
        assert item["id"] == img_id
        assert item["filename"] == "shape_test.jpg"
        assert item["url"] == f"/api/stock-images/{img_id}/file"
        assert item["category_id"] == cat_id
        assert item["category_name"] == "结构测试栏目"
        assert item["kind"] == "main"
    finally:
        app.cleanup()


# ── latest_image_at ───────────────────────────────────────────────────────


@pytest.mark.mysql
def test_list_categories_latest_image_at_with_images(monkeypatch):
    """有图的栏目返回最新图片的 created_at。"""
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            cat = _insert_category(db, "有图栏目", "has-images-bucket", "companion")
            _insert_image(db, cat.id, "older.jpg")
            _insert_image(db, cat.id, "newer.jpg")
            db.commit()
            cat_id = cat.id

        r = app.client.get("/api/image-library/categories")
        assert r.status_code == 200, r.text
        categories = r.json()
        cat_data = next((c for c in categories if c["id"] == cat_id), None)
        assert cat_data is not None
        assert cat_data["latest_image_at"] is not None
        # 应该是 ISO 格式时间字符串
        assert isinstance(cat_data["latest_image_at"], str)
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_list_categories_latest_image_at_empty_category(monkeypatch):
    """没有图片的栏目 latest_image_at 为 None。"""
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            cat = _insert_category(db, "空栏目", "empty-cat-bucket", "companion")
            db.commit()
            cat_id = cat.id

        r = app.client.get("/api/image-library/categories")
        assert r.status_code == 200, r.text
        categories = r.json()
        cat_data = next((c for c in categories if c["id"] == cat_id), None)
        assert cat_data is not None
        assert cat_data["latest_image_at"] is None
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_list_categories_latest_image_at_is_max(monkeypatch):
    """latest_image_at 是该栏目中 created_at 最大的那张图的时间。

    created_at 是秒级精度 DATETIME + Python-side default，同一次 flush 内的两行
    可能落同一秒。因此直接给两张图设显式、相差 60s 的 created_at，再断言
    latest_image_at 精确等于较晚那张的时间（秒级）。
    """
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)

        # 使用固定、明确分离的时间戳（秒级截断，避免 Python 侧毫秒扰动）
        base_time = datetime(2020, 1, 1, 12, 0, 0, tzinfo=UTC)
        older_time = base_time  # 2020-01-01T12:00:00Z
        newer_time = base_time + timedelta(seconds=60)  # 2020-01-01T12:01:00Z

        with app.session_factory() as db:
            cat = _insert_category(db, "最新时间栏目", "max-time-bucket", "companion")
            # 显式指定 created_at，绕过 Python-side default
            _insert_image(db, cat.id, "first.jpg", created_at=older_time.replace(tzinfo=None))
            _insert_image(db, cat.id, "latest.jpg", created_at=newer_time.replace(tzinfo=None))
            db.commit()
            cat_id = cat.id

        r = app.client.get("/api/image-library/categories")
        assert r.status_code == 200, r.text
        categories = r.json()
        cat_data = next((c for c in categories if c["id"] == cat_id), None)
        assert cat_data is not None
        assert cat_data["latest_image_at"] is not None

        # latest_image_at 必须精确等于较晚那张图的 created_at（秒级）
        returned = datetime.fromisoformat(cat_data["latest_image_at"].replace("Z", "+00:00"))
        expected = newer_time
        diff = abs((returned - expected).total_seconds())
        assert diff < 1, (
            f"latest_image_at 应等于较晚图的时间戳，实际差: {diff}s (returned={returned}, expected={expected})"
        )
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_list_categories_latest_image_at_mixed(monkeypatch):
    """同时有有图栏目和无图栏目，两者字段都正确。"""
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            cat_with = _insert_category(db, "有图的", "with-imgs", "companion")
            cat_empty = _insert_category(db, "没图的", "without-imgs", "companion")
            _insert_image(db, cat_with.id, "img.jpg")
            db.commit()
            cat_with_id = cat_with.id
            cat_empty_id = cat_empty.id

        r = app.client.get("/api/image-library/categories")
        assert r.status_code == 200, r.text
        categories = r.json()
        by_id = {c["id"]: c for c in categories}
        assert by_id[cat_with_id]["latest_image_at"] is not None
        assert by_id[cat_empty_id]["latest_image_at"] is None
    finally:
        app.cleanup()
