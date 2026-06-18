# 图片库栏目删除 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户能在图片库选中一个栏目后删除整个栏目（清一整桶图），带二次确认窗 + 被文章引用的预警。

**Architecture:** 硬删——后端放开「非空栏目禁删」限制，先解开 `articles.stock_category_id` 外键引用、清空 MinIO 桶、删桶、级联删图片记录、删栏目记录；新增一个 delete-preview 端点扫描「平台内仍引用本栏目图片的文章数」给确认窗预警。前端在 `ImageLibraryWorkspace` 顶栏加 danger 删除按钮 + 确认 modal。

**Tech Stack:** FastAPI + SQLAlchemy（MySQL）、MinIO（minio-py）、React 19 + TypeScript + Tiptap。

## Global Constraints

- 后端 service/路由层抛命名异常（`ClientError`/`ConflictError`），不抛裸 `ValueError`；本计划用 `HTTPException`（路由层）。
- 所有模型调用走 LiteLLM（本特性不涉及 AI）。
- 后端测试 MySQL only，需 `GEO_TEST_DATABASE_URL`（库名含 "test"），测试标 `@pytest.mark.mysql`，用 `build_test_app(monkeypatch)`，`finally` 里 `app.cleanup()`。
- 前端无单测框架，门禁 = `pnpm --filter @geo/web typecheck` + `build`；端口约束与本特性无关。
- ruff 选 E/F/I/B/UP，line-length=100，忽略 E501/B008。
- **工作区是 worktree** `E:\geo\.claude\worktrees\image-library-category-delete`。注意 Windows 上 worktree 内 Bash cwd 会漂回主 checkout `/e/geo`——所有 git/pnpm/pytest 命令用 `git -C <worktree>` / `pnpm -C <worktree>/web` / 绝对路径，跑完 `pwd` 确认。
- 已激活 conda 环境 `geo_xzpt` 后再跑 Python（工具 shell 里 `conda activate` 可能不生效，必要时用 env 的 python 全路径）。

---

## File Structure

- `server/app/modules/image_library/store.py` — 加 `empty_bucket()`（清桶对象）。
- `server/app/modules/image_library/router.py` — 改写 `delete_category`（放开限制 + FK 清理 + 硬删）；新增 `GET /categories/{id}/delete-preview` 端点 + 引用扫描；加 `logger`、import `Article`。
- `server/tests/test_image_library_delete.py` — 新建，覆盖硬删 + 预览。
- `web/src/api/image-library.ts` — 加 `getCategoryDeletePreview()`。
- `web/src/features/image-library/ImageLibraryWorkspace.tsx` — 顶栏删除按钮 + 确认 modal + 处理函数。
- `web/src/styles.css` — 加确认窗预警/安全两行文字样式。

---

## Task 1: 后端硬删栏目（store.empty_bucket + delete_category 改写）

**Files:**
- Modify: `server/app/modules/image_library/store.py`（加 `empty_bucket`）
- Modify: `server/app/modules/image_library/router.py:291-323`（改写 `delete_category`，加 import/logger）
- Test: `server/tests/test_image_library_delete.py`（新建）

**Interfaces:**
- Produces: `minio_store.empty_bucket(bucket_name: str) -> None`；`DELETE /api/image-library/categories/{id}` 现在能删非空栏目，返回 204。
- Consumes: 现有 `StockCategory`/`StockImage` 模型、`articles.models.Article`、`add_audit_entry`。

- [ ] **Step 1: 写失败测试**

新建 `server/tests/test_image_library_delete.py`：

```python
"""图片库栏目硬删 + 删除预览测试。

§测试 清单：
- 删非空栏目：204、桶被清空+删除、图片记录级联删、栏目删除
- FK 清理：引用本栏目的 articles.stock_category_id 置 NULL（文章不被删）
- M2M 清理：article_stock_categories join 行随栏目级联删，文章存活
- MinIO best-effort：empty_bucket 抛错仍删 DB 记录
- 删不存在栏目 → 404
- delete-preview：有/无引用计数正确、软删文章不计、prefix 不误中、404
"""

import pytest

from server.app.modules.articles.models import Article
from server.app.modules.image_library.models import StockCategory, StockImage
from server.app.modules.system.models import User
from server.tests.utils import build_test_app


def _patch_minio(monkeypatch, calls=None):
    """无 MinIO：建桶/上传 no-op；清桶/删桶记录调用到 calls（若传）。"""
    monkeypatch.setattr(
        "server.app.modules.image_library.router.minio_store.ensure_bucket",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "server.app.modules.image_library.router.minio_store.upload_image",
        lambda *a, **k: None,
    )

    def _empty(bucket):
        if calls is not None:
            calls.setdefault("empty", []).append(bucket)

    def _remove(bucket):
        if calls is not None:
            calls.setdefault("remove", []).append(bucket)

    monkeypatch.setattr(
        "server.app.modules.image_library.router.minio_store.empty_bucket", _empty
    )
    monkeypatch.setattr(
        "server.app.modules.image_library.router.minio_store.remove_bucket", _remove
    )


def _insert_category(db, name, bucket, kind="companion"):
    cat = StockCategory(name=name, bucket_name=bucket, kind=kind)
    db.add(cat)
    db.flush()
    return cat


def _insert_image(db, category_id, filename):
    img = StockImage(
        category_id=category_id, minio_key=f"key-{filename}", filename=filename, tags=[]
    )
    db.add(img)
    db.flush()
    return img


def _user_id(db):
    return db.query(User).first().id


@pytest.mark.mysql
def test_delete_non_empty_category(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        calls = {}
        _patch_minio(monkeypatch, calls)
        with app.session_factory() as db:
            cat = _insert_category(db, "待删栏目", "del-bucket", "companion")
            _insert_image(db, cat.id, "a.jpg")
            _insert_image(db, cat.id, "b.jpg")
            db.commit()
            cat_id = cat.id

        r = app.client.delete(f"/api/image-library/categories/{cat_id}")
        assert r.status_code == 204, r.text

        with app.session_factory() as db:
            assert db.get(StockCategory, cat_id) is None
            assert db.query(StockImage).filter(StockImage.category_id == cat_id).count() == 0
        # MinIO 清桶 + 删桶都被调用
        assert calls.get("empty") == ["del-bucket"]
        assert calls.get("remove") == ["del-bucket"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_delete_category_nulls_article_fk(monkeypatch):
    """引用本栏目的 articles.stock_category_id 被置 NULL，文章本身不删。"""
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            uid = _user_id(db)
            cat = _insert_category(db, "FK栏目", "fk-bucket", "main")
            db.flush()
            art = Article(user_id=uid, title="引用了主推栏目", stock_category_id=cat.id)
            db.add(art)
            db.commit()
            cat_id = cat.id
            art_id = art.id

        r = app.client.delete(f"/api/image-library/categories/{cat_id}")
        assert r.status_code == 204, r.text

        with app.session_factory() as db:
            refreshed = db.get(Article, art_id)
            assert refreshed is not None  # 文章没被删
            assert refreshed.stock_category_id is None  # FK 被置空
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_delete_category_clears_m2m(monkeypatch):
    """article_stock_categories join 行随栏目级联删（ON DELETE CASCADE），文章存活。"""
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            uid = _user_id(db)
            cat = _insert_category(db, "M2M栏目", "m2m-bucket", "companion")
            db.flush()
            art = Article(user_id=uid, title="多对多关联")
            art.stock_categories.append(cat)
            db.add(art)
            db.commit()
            cat_id = cat.id
            art_id = art.id

        r = app.client.delete(f"/api/image-library/categories/{cat_id}")
        assert r.status_code == 204, r.text

        with app.session_factory() as db:
            refreshed = db.get(Article, art_id)
            assert refreshed is not None
            assert all(c.id != cat_id for c in refreshed.stock_categories)
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_delete_category_minio_error_still_deletes_db(monkeypatch):
    """empty_bucket 抛错时仍删 DB 记录（best-effort 不阻断）。"""
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)

        def _boom(bucket):
            raise RuntimeError("minio down")

        monkeypatch.setattr(
            "server.app.modules.image_library.router.minio_store.empty_bucket", _boom
        )
        with app.session_factory() as db:
            cat = _insert_category(db, "MinIO炸栏目", "boom-bucket", "companion")
            _insert_image(db, cat.id, "x.jpg")
            db.commit()
            cat_id = cat.id

        r = app.client.delete(f"/api/image-library/categories/{cat_id}")
        assert r.status_code == 204, r.text
        with app.session_factory() as db:
            assert db.get(StockCategory, cat_id) is None
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_delete_nonexistent_category_404(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        r = app.client.delete("/api/image-library/categories/999999")
        assert r.status_code == 404, r.text
    finally:
        app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run（worktree 根目录）：
```bash
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test \
  pytest server/tests/test_image_library_delete.py -q -k "delete_non_empty or nulls_article_fk or clears_m2m or minio_error or nonexistent"
```
Expected: FAIL —— `delete_non_empty_category` 当前因「有图 → 409」断言失败（实际 409 ≠ 204），且 `minio_store.empty_bucket` 属性不存在（AttributeError on monkeypatch.setattr）。

- [ ] **Step 3: 加 `empty_bucket` 到 store.py**

在 `server/app/modules/image_library/store.py` 的 `remove_bucket` 之前加：

```python
def empty_bucket(bucket_name: str) -> None:
    """删除桶内所有对象（删桶前先清空，MinIO 仅允许删空桶）。

    list_objects(recursive=True) 列出所有对象 key，逐个 remove_object。
    best-effort 语义由调用方决定（router 捕获异常不阻断）。
    """
    client = _client()
    for obj in client.list_objects(bucket_name, recursive=True):
        client.remove_object(bucket_name, obj.object_name)
```

- [ ] **Step 4: 改写 router.py 的 `delete_category` + 加 import/logger**

在 `server/app/modules/image_library/router.py` 顶部 import 区加：
```python
import logging
```
并在 import 块后（`router = APIRouter()` 之前）加：
```python
logger = logging.getLogger(__name__)
```
在 `from server.app.modules.image_library.models import StockCategory, StockImage` 之后加：
```python
from server.app.modules.articles.models import Article
```

把现有 `delete_category`（约 291-323 行）整体替换为：

```python
@router.delete("/categories/{category_id}", status_code=204)
def delete_category(
    category_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    cat = db.get(StockCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="栏目不存在")

    image_count = db.query(StockImage).filter(StockImage.category_id == category_id).count()
    cat_name = cat.name
    bucket_name = cat.bucket_name

    # 先解开指向本栏目的单值外键引用：articles.stock_category_id 的 FK 无 ON DELETE，
    # 默认 RESTRICT，不置空会让 db.delete(cat) 触发 MySQL 1451。多对多 article_stock_categories
    # 的 FK 带 ON DELETE CASCADE，由 DB 自动清理 join 行，无需手动处理。
    db.query(Article).filter(Article.stock_category_id == category_id).update(
        {Article.stock_category_id: None}, synchronize_session=False
    )

    # MinIO best-effort：清桶 + 删桶失败只 log warning 不阻断（与 delete_image 同哲学，
    # 以 DB 记录为准，宁可残留孤儿对象/空桶——桶名自动唯一生成不影响后续建桶）。
    try:
        minio_store.empty_bucket(bucket_name)
    except Exception:
        logger.warning("清空 bucket 失败，残留对象待清理: %s", bucket_name, exc_info=True)
    try:
        minio_store.remove_bucket(bucket_name)
    except Exception:
        logger.warning("删除 bucket 失败，残留空桶待清理: %s", bucket_name, exc_info=True)

    db.delete(cat)  # cascade="all, delete-orphan" 删该栏目所有 StockImage 记录
    db.commit()
    add_audit_entry(
        db,
        user=current_user,
        action="stock_category.delete",
        target_type="stock_category",
        target_id=category_id,
        payload={"name": cat_name, "image_count": image_count},
        request=request,
    )
```

- [ ] **Step 5: 跑测试确认通过**

Run：
```bash
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test \
  pytest server/tests/test_image_library_delete.py -q -k "delete_non_empty or nulls_article_fk or clears_m2m or minio_error or nonexistent"
```
Expected: 5 passed.

- [ ] **Step 6: ruff + commit**

Run：
```bash
ruff check server/app/modules/image_library/ server/tests/test_image_library_delete.py
ruff format server/app/modules/image_library/ server/tests/test_image_library_delete.py
```
Then：
```bash
git -C /e/geo/.claude/worktrees/image-library-category-delete add \
  server/app/modules/image_library/store.py \
  server/app/modules/image_library/router.py \
  server/tests/test_image_library_delete.py
git -C /e/geo/.claude/worktrees/image-library-category-delete commit -m "feat(image-library): 栏目硬删——清桶+级联删+FK 清理

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: 后端删除预览端点（引用扫描）

**Files:**
- Modify: `server/app/modules/image_library/router.py`（加 `CategoryDeletePreview` 模型 + `category_delete_preview` 端点 + 正则常量）
- Test: `server/tests/test_image_library_delete.py`（追加预览用例）

**Interfaces:**
- Produces: `GET /api/image-library/categories/{id}/delete-preview` → `{ image_count: int, referenced_article_count: int | null }`。
- Consumes: Task 1 的 import（`Article`、`logger`、`re`）。

- [ ] **Step 1: 追加失败测试**

在 `server/tests/test_image_library_delete.py` 末尾追加：

```python
def _insert_article_html(db, uid, title, html, *, is_deleted=False):
    art = Article(user_id=uid, title=title, content_html=html, is_deleted=is_deleted)
    db.add(art)
    db.flush()
    return art


@pytest.mark.mysql
def test_delete_preview_counts_references(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            uid = _user_id(db)
            cat = _insert_category(db, "预览栏目", "preview-bucket", "companion")
            other = _insert_category(db, "别的栏目", "other-bucket", "companion")
            img1 = _insert_image(db, cat.id, "p1.jpg")
            img2 = _insert_image(db, cat.id, "p2.jpg")
            other_img = _insert_image(db, other.id, "o1.jpg")
            db.flush()
            # 引用本栏目 img1 —— 计入
            _insert_article_html(
                db, uid, "用了图1",
                f'<p><img src="/api/stock-images/{img1.id}/file"></p>',
            )
            # 引用本栏目 img2 —— 计入（另一篇）
            _insert_article_html(
                db, uid, "用了图2",
                f'<img src="/api/stock-images/{img2.id}/file">',
            )
            # 引用别的栏目的图 —— 不计入
            _insert_article_html(
                db, uid, "用了别栏目",
                f'<img src="/api/stock-images/{other_img.id}/file">',
            )
            # 软删文章引用 img1 —— 不计入
            _insert_article_html(
                db, uid, "软删的", f'<img src="/api/stock-images/{img1.id}/file">',
                is_deleted=True,
            )
            # prefix 不误中：引用 "{img1.id}9"（不存在的 id），不应误判为 img1
            _insert_article_html(
                db, uid, "prefix干扰",
                f'<img src="/api/stock-images/{img1.id}9/file">',
            )
            db.commit()
            cat_id = cat.id

        r = app.client.get(f"/api/image-library/categories/{cat_id}/delete-preview")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["image_count"] == 2
        assert body["referenced_article_count"] == 2
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_delete_preview_zero_references(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        with app.session_factory() as db:
            cat = _insert_category(db, "无引用栏目", "noref-bucket", "companion")
            _insert_image(db, cat.id, "n1.jpg")
            db.commit()
            cat_id = cat.id

        r = app.client.get(f"/api/image-library/categories/{cat_id}/delete-preview")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["image_count"] == 1
        assert body["referenced_article_count"] == 0
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_delete_preview_404(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _patch_minio(monkeypatch)
        r = app.client.get("/api/image-library/categories/999999/delete-preview")
        assert r.status_code == 404, r.text
    finally:
        app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run：
```bash
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test \
  pytest server/tests/test_image_library_delete.py -q -k preview
```
Expected: FAIL —— 端点不存在，`/categories/999999/delete-preview` 会被 `/categories/{category_id}` 之外的路由匹配或 404，但 `test_delete_preview_counts_references` 会拿到 404/405 而非 200。

- [ ] **Step 3: 加预览端点 + 正则常量**

在 `server/app/modules/image_library/router.py` 顶部 import 区加：
```python
import re
```
在 `# ── 辅助函数 ─` 区加一个模块级正则常量：
```python
_STOCK_IMG_URL_RE = re.compile(r"/api/stock-images/(\d+)/file")
```
在 `# ── 出参模型 ─` 附近（如 `SearchResultRead` 之后）加：
```python
class CategoryDeletePreview(BaseModel):
    image_count: int
    referenced_article_count: int | None
```
在 `delete_category` 之后加端点：

```python
@router.get("/categories/{category_id}/delete-preview", response_model=CategoryDeletePreview)
def category_delete_preview(
    category_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Any:
    """删除栏目前的引用预览：图片数 + 平台内仍引用本栏目图片的（未软删）文章数。

    引用扫描全表 LIKE 预筛 + Python 正则精确交集，best-effort：扫描异常返回
    referenced_article_count=None，前端提示「统计失败」但不阻断删除。
    """
    cat = db.get(StockCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="栏目不存在")

    image_ids = {
        row[0] for row in db.query(StockImage.id).filter(StockImage.category_id == category_id)
    }
    image_count = len(image_ids)

    referenced_article_count: int | None
    try:
        referenced = 0
        rows = db.query(Article.content_html).filter(
            Article.content_html.like("%/api/stock-images/%"),
            Article.is_deleted.is_(False),
        )
        for (content_html,) in rows:
            if not content_html:
                continue
            ids_in_article = {int(m) for m in _STOCK_IMG_URL_RE.findall(content_html)}
            if ids_in_article & image_ids:
                referenced += 1
        referenced_article_count = referenced
    except Exception:
        logger.warning("统计栏目引用文章数失败: category_id=%s", category_id, exc_info=True)
        referenced_article_count = None

    return CategoryDeletePreview(
        image_count=image_count, referenced_article_count=referenced_article_count
    )
```

> 路由放在 `delete_category` 之后即可：路径 `/categories/{id}/delete-preview` 比 `/categories/{id}` 多一段后缀，FastAPI 按全路径匹配，无遮蔽。

- [ ] **Step 4: 跑测试确认通过**

Run：
```bash
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test \
  pytest server/tests/test_image_library_delete.py -q
```
Expected: 8 passed（Task 1 的 5 + 本任务 3）。

- [ ] **Step 5: ruff + commit**

```bash
ruff check server/app/modules/image_library/router.py server/tests/test_image_library_delete.py
ruff format server/app/modules/image_library/router.py server/tests/test_image_library_delete.py
git -C /e/geo/.claude/worktrees/image-library-category-delete add \
  server/app/modules/image_library/router.py server/tests/test_image_library_delete.py
git -C /e/geo/.claude/worktrees/image-library-category-delete commit -m "feat(image-library): 栏目删除预览端点（引用文章扫描）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: 前端删除按钮 + 确认 modal

**Files:**
- Modify: `web/src/api/image-library.ts`（加 `getCategoryDeletePreview`）
- Modify: `web/src/features/image-library/ImageLibraryWorkspace.tsx`（状态 + 处理函数 + 按钮 + modal）
- Modify: `web/src/styles.css`（预警/安全文字样式）

**Interfaces:**
- Consumes: Task 2 的 `GET /categories/{id}/delete-preview`；现有 `deleteCategory`。
- Produces: 无（终端 UI）。

- [ ] **Step 1: API 客户端加 `getCategoryDeletePreview`**

在 `web/src/api/image-library.ts` 的 `deleteCategory` 之后加：

```typescript
export function getCategoryDeletePreview(
  categoryId: number,
): Promise<{ image_count: number; referenced_article_count: number | null }> {
  return api(`/api/image-library/categories/${categoryId}/delete-preview`);
}
```

- [ ] **Step 2: import + 新增状态**

在 `ImageLibraryWorkspace.tsx` 第 3 行的 api import 里追加 `deleteCategory, getCategoryDeletePreview`（与现有 `createCategory, ...` 同一行 import）。

在 `editingCategory` 相关 state 之后（约 35 行附近）加：

```tsx
  const [deletingCategory, setDeletingCategory] = useState<StockCategory | null>(null);
  const [deletePreview, setDeletePreview] = useState<{
    image_count: number;
    referenced_article_count: number | null;
  } | null>(null);
  const [deletePreviewLoading, setDeletePreviewLoading] = useState(false);
  const [deleteSaving, setDeleteSaving] = useState(false);
```

- [ ] **Step 3: 加处理函数**

在 `handleSaveCategoryEdit` 之后加：

```tsx
  function openDeleteCategory(category: StockCategory) {
    setDeletingCategory(category);
    setDeletePreview(null);
    setDeletePreviewLoading(true);
    getCategoryDeletePreview(category.id)
      .then(setDeletePreview)
      .catch(() => setDeletePreview(null))
      .finally(() => setDeletePreviewLoading(false));
  }

  async function handleConfirmDeleteCategory() {
    if (!deletingCategory) return;
    const deletedId = deletingCategory.id;
    setDeleteSaving(true);
    try {
      await deleteCategory(deletedId);
      const remaining = categories.filter((c) => c.id !== deletedId);
      setCategories(remaining);
      if (selectedCategoryId === deletedId) {
        setSelectedCategoryId(remaining.length > 0 ? remaining[0].id : null);
      }
      setDeletingCategory(null);
      setDeletePreview(null);
      showToast("栏目已删除", "success");
    } catch (e: unknown) {
      showToast((e as Error).message, "error");
    } finally {
      setDeleteSaving(false);
    }
  }
```

- [ ] **Step 4: 顶栏加删除按钮**

在「编辑栏目」按钮（约 506-513 行 `<Pencil size={15} /> 编辑栏目` 那个 button）之后、「上传图片」按钮之前加：

```tsx
          <button
            type="button"
            className="dangerButton"
            disabled={!selectedCategory}
            onClick={() => {
              if (selectedCategory) openDeleteCategory(selectedCategory);
            }}
          >
            <Trash2 size={15} /> 删除栏目
          </button>
```

（`Trash2` 已在第 2 行 import，无需改 import。）

- [ ] **Step 5: 加确认 modal**

在 `editingCategory` 的 modal 块（以 `{editingCategory && (` 开头那段）的闭合 `)}` 之后加：

```tsx
      {deletingCategory && (
        <div className="modalOverlay" onClick={() => { if (!deleteSaving) setDeletingCategory(null); }}>
          <div className="modalCard" onClick={(e) => e.stopPropagation()}>
            <h2>删除栏目</h2>
            <p>
              确定删除栏目「<strong>{deletingCategory.name}</strong>」？
              {deletePreviewLoading
                ? "（正在统计…）"
                : deletePreview
                  ? ` 该栏目含 ${deletePreview.image_count} 张图片，将被一并永久删除。`
                  : ""}
            </p>
            {!deletePreviewLoading && deletePreview && (
              deletePreview.referenced_article_count === null ? (
                <p className="imageLibraryDeleteWarn">引用统计失败，请谨慎删除。</p>
              ) : deletePreview.referenced_article_count > 0 ? (
                <p className="imageLibraryDeleteWarn">
                  ⚠ 有 {deletePreview.referenced_article_count} 篇平台内文章正引用本栏目图片，
                  删除后它们在平台内会显示裂图（已发布到外部平台的不受影响）。
                </p>
              ) : (
                <p className="imageLibraryDeleteSafe">无文章引用，可安全删除。</p>
              )
            )}
            <div className="modalActions">
              <button type="button" className="secondaryButton" onClick={() => setDeletingCategory(null)} disabled={deleteSaving}>
                取消
              </button>
              <button type="button" className="dangerButton" onClick={handleConfirmDeleteCategory} disabled={deleteSaving}>
                {deleteSaving ? "删除中..." : "确认删除"}
              </button>
            </div>
          </div>
        </div>
      )}
```

- [ ] **Step 6: 加两行文字样式**

在 `web/src/styles.css` 末尾的 imageLibrary 区（如 `.imageLibraryDropdown button.danger` 附近）加：

```css
.imageLibraryDeleteWarn { color: #e53e3e; font-size: 13px; line-height: 1.5; margin: 0; }
.imageLibraryDeleteSafe { color: #6b7280; font-size: 13px; margin: 0; }
```

- [ ] **Step 7: typecheck + build**

Run：
```bash
pnpm -C /e/geo/.claude/worktrees/image-library-category-delete/web install
pnpm -C /e/geo/.claude/worktrees/image-library-category-delete/web typecheck
pnpm -C /e/geo/.claude/worktrees/image-library-category-delete/web build
```
Expected: typecheck 0 errors；build 成功。

- [ ] **Step 8: commit**

```bash
git -C /e/geo/.claude/worktrees/image-library-category-delete add \
  web/src/api/image-library.ts \
  web/src/features/image-library/ImageLibraryWorkspace.tsx \
  web/src/styles.css
git -C /e/geo/.claude/worktrees/image-library-category-delete commit -m "feat(image-library): 前端栏目删除按钮 + 确认窗（带引用预警）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 收尾

- 跑全套后端图片库相关测试确认无回归：
  ```bash
  GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test \
    pytest server/tests/test_image_library_delete.py server/tests/test_image_library_search.py -q
  ```
- mypy（宽松）：`mypy server/app/modules/image_library` 不报新错。
- 完成后用 finishing-a-development-branch 决定合并/PR 方式（worktree → 推分支 → PR）。

## 自检（spec coverage 对照）

- spec「store.empty_bucket」→ Task 1 Step 3 ✓
- spec「delete_category 去 409 + FK 清理 + best-effort 清桶删桶 + 级联 + audit」→ Task 1 Step 4 ✓
- spec「delete-preview 端点 + 引用扫描 + best-effort null」→ Task 2 ✓
- spec「前端 API 客户端 + 删除按钮 + 确认 modal（统计中/有引用/无引用/失败四态）」→ Task 3 ✓
- spec「搜索/列表不改」→ 本计划不动 search/list，✓（无需任务）
- spec「权限=任何登录用户」→ 端点维持 `Depends(get_current_user)`，Task 1/2 未加 require_admin ✓
- spec「测试四类 + 404」→ Task 1（5 例）+ Task 2（3 例）✓
```
