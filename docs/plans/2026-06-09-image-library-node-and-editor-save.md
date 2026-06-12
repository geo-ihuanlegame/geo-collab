# 图片库主推/陪衬 + AI配图节点 + 编辑器图片保存 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给图片库加「主推/陪衬」分类，把配图抽成可组合的 pipeline 节点（复用 AI格式 后端），编辑器去掉 AI格式/图片栏目、改成「图片保存」。

**Architecture:** `StockCategory` 加 `kind` 列（main/companion）→ 新节点 `ai_illustrate` 把「主推栏目 + 全部陪衬栏目」喂给现有 `run_ai_format` 由 AI 统一决定插图 → 前端图片库两 tab、节点配置面板、编辑器 WPS 保存框。分 PR1–PR4，PR1 是地基，PR2/3/4 依赖 PR1。

**Tech Stack:** FastAPI + SQLAlchemy + Alembic（MySQL only）、pytest（`@pytest.mark.mysql` + `build_test_app`）、React 19 + Vite + TS + Tiptap。

**对应规格：** [docs/specs/2026-06-09-image-library-node-and-editor-save-design.md](../specs/2026-06-09-image-library-node-and-editor-save-design.md)

**全程不修改 `demo.pen`。**

---

## 环境前置（每次开 pytest 前）

- conda 环境：`conda activate geo_xzpt`（工具 shell 里可能不生效，用 env python 全路径跑 pytest——见内存 `run-tests-env`）。
- 测试 DB：`GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test`（库名须含 `test`）。
- 开发 DB（跑 alembic 用）：`GEO_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_dev`。
- 前端门禁：`pnpm --filter @geo/web typecheck` + `pnpm --filter @geo/web build`（本仓库前端无单测，typecheck+build 即门禁）。

---

## 文件结构（决策锁定）

| 文件 | 责任 | PR |
|------|------|----|
| `server/app/modules/image_library/models.py` | `StockCategory.kind` 列 | PR1 |
| `server/alembic/versions/0043_stock_category_kind.py` | 加 `kind` 列 + CHECK | PR1 |
| `server/app/modules/image_library/router.py` | schema `kind` + `?kind=` 过滤 | PR1 |
| `server/tests/test_image_library_kind.py` | kind 创建/默认/过滤测试 | PR1 |
| `server/app/modules/articles/ai_format.py` | 新增 `category_contexts_for` helper | PR2 |
| `server/app/modules/pipelines/nodes/ai_illustrate.py` | 新节点 | PR2 |
| `server/app/modules/pipelines/nodes/__init__.py` | import 触发注册 | PR2 |
| `server/tests/test_ai_illustrate_node.py` | 候选栏目 + 透传 + 失败聚合测试 | PR2 |
| `web/src/types.ts` | `StockCategory.kind` | PR3 |
| `web/src/api/image-library.ts` | `kind` 字段 + `?kind=` 查询 | PR3 |
| `web/src/features/image-library/ImageLibraryWorkspace.tsx` | 主推/陪衬 tabs | PR3 |
| `web/src/api/pipelines.ts` + `web/src/features/pipelines/PipelineEditor.tsx` | 「AI配图」节点类型 + 配置面板 | PR3 |
| `web/src/components/editor/EditorToolbar.tsx` | 删 AI格式、加图片保存按钮 | PR4 |
| `web/src/components/editor/ImageSaveDialog.tsx` | WPS 风格保存框（新） | PR4 |
| `web/src/features/content/ContentWorkspace.tsx` | 删图片栏目选择 + 接保存框 | PR4 |

> 注：`build_test_app` 用 `Base.metadata.create_all` 建表（非 alembic），所以**模型列**改动即被测试感知；**迁移文件**由 `alembic upgrade head`（PR1 Task 3）+ CI 单独验证。

---

# PR1 — 图片库 `kind` 字段（地基）

### Task 1: 写失败的路由测试

**Files:**
- Test: `server/tests/test_image_library_kind.py`（Create）

- [ ] **Step 1: 写测试**

```python
import pytest

from server.tests.utils import build_test_app


def _make_cat(client, name, bucket, kind=None):
    body = {"name": name, "bucket_name": bucket}
    if kind is not None:
        body["kind"] = kind
    r = client.post("/api/image-library/categories", json=body)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.mysql
def test_category_kind_create_default_and_filter(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        client = app.client
        # 测试环境无 MinIO：把建桶打成 no-op
        monkeypatch.setattr(
            "server.app.modules.image_library.router.minio_store.ensure_bucket",
            lambda *a, **k: None,
        )
        m = _make_cat(client, "主推游戏A", "main-a", "main")
        c = _make_cat(client, "陪衬游戏B", "comp-b", "companion")
        d = _make_cat(client, "默认C", "def-c")  # 省略 kind → 默认 companion
        assert m["kind"] == "main"
        assert c["kind"] == "companion"
        assert d["kind"] == "companion"

        main_ids = {x["id"] for x in client.get("/api/image-library/categories?kind=main").json()}
        comp_ids = {x["id"] for x in client.get("/api/image-library/categories?kind=companion").json()}
        all_ids = {x["id"] for x in client.get("/api/image-library/categories").json()}

        assert main_ids == {m["id"]}
        assert {c["id"], d["id"]} <= comp_ids and m["id"] not in comp_ids
        assert {m["id"], c["id"], d["id"]} <= all_ids
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_category_kind_reclassify(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        client = app.client
        monkeypatch.setattr(
            "server.app.modules.image_library.router.minio_store.ensure_bucket",
            lambda *a, **k: None,
        )
        c = _make_cat(client, "栏目X", "x-bucket", "companion")
        r = client.patch(f"/api/image-library/categories/{c['id']}", json={"kind": "main"})
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "main"
    finally:
        app.cleanup()
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest server/tests/test_image_library_kind.py -q`
Expected: FAIL（`kind` 未被接受/返回；可能 422 或 KeyError `kind`）。

---

### Task 2: 实现 `kind`（模型 + schema + 路由）

**Files:**
- Modify: `server/app/modules/image_library/models.py:12-27`
- Modify: `server/app/modules/image_library/router.py`（CategoryCreate/Read/Update、create/list/update、_to_category_read）

- [ ] **Step 1: 模型加列**

`server/app/modules/image_library/models.py`，`StockCategory` 内 `bucket_name` 之后加：

```python
    kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default="companion")
    # 'main'=主推（手选一个）/ 'companion'=陪衬（AI 按文章检测）
```

- [ ] **Step 2: schema 加 `kind`**

`server/app/modules/image_library/router.py`：

`CategoryCreate` 加字段 + 校验：
```python
    kind: str = "companion"

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str) -> str:
        if value not in {"main", "companion"}:
            raise ValueError("kind must be 'main' or 'companion'")
        return value
```

`CategoryUpdate` 加可选字段 + 校验：
```python
    kind: str | None = None

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str | None) -> str | None:
        if value is not None and value not in {"main", "companion"}:
            raise ValueError("kind must be 'main' or 'companion'")
        return value
```

`CategoryRead` 加字段：
```python
    kind: str
```

- [ ] **Step 3: `_to_category_read` 带上 kind**

```python
def _to_category_read(cat: StockCategory) -> CategoryRead:
    return CategoryRead(
        id=cat.id,
        name=cat.name,
        bucket_name=cat.bucket_name,
        kind=cat.kind,
        description=cat.description,
        official_url=cat.official_url,
        created_at=cat.created_at,
    )
```

- [ ] **Step 4: create_category 写入 kind**

`create_category` 里构造 `StockCategory(...)` 加 `kind=payload.kind,`。

- [ ] **Step 5: list_categories 支持 `?kind=`**

```python
@router.get("/categories", response_model=list[CategoryRead])
def list_categories(
    kind: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Any:
    q = db.query(StockCategory)
    if kind in {"main", "companion"}:
        q = q.filter(StockCategory.kind == kind)
    cats = q.order_by(StockCategory.created_at.desc()).all()
    return [_to_category_read(c) for c in cats]
```

- [ ] **Step 6: update_category 支持改 kind**

`update_category` 内，`official_url` 处理之后加：
```python
    if "kind" in update_data and update_data["kind"] is not None:
        cat.kind = update_data["kind"]
```

- [ ] **Step 7: 运行测试确认通过**

Run: `python -m pytest server/tests/test_image_library_kind.py -q`
Expected: PASS（2 passed）。

- [ ] **Step 8: 回归 image-library 相关测试**

Run: `python -m pytest server/tests/test_image_library_inserter.py -q`
Expected: PASS（未受影响）。

---

### Task 3: 迁移 `0043`

**Files:**
- Create: `server/alembic/versions/0043_stock_category_kind.py`

- [ ] **Step 1: 写迁移**

```python
"""图片库栏目加 kind（主推/陪衬）

修订 ID: 0043
上一修订: 0042
创建日期: 2026-06-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stock_categories",
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="companion"),
    )
    op.create_check_constraint(
        "ck_stock_categories_kind",
        "stock_categories",
        "kind in ('main', 'companion')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_stock_categories_kind", "stock_categories", type_="check")
    op.drop_column("stock_categories", "kind")
```

- [ ] **Step 2: 对开发库跑迁移验证**

Run: `alembic upgrade head`（需 `GEO_DATABASE_URL` 指向 geo_dev）
Expected: 无报错，末行应用 `0043`。再跑 `alembic current` 应显示 `0043 (head)`。

- [ ] **Step 3: 验证可回滚**

Run: `alembic downgrade -1` 然后 `alembic upgrade head`
Expected: 两次都成功（验证 downgrade 正确）。

---

### Task 4: PR1 收尾（分支 + 提交 + PR）

- [ ] **Step 1: 建分支**（当前在 main，提交前必须开分支）

```bash
git checkout -b feat/image-library-kind
```

- [ ] **Step 2: 后端门禁**

Run:
```bash
ruff check server/ && ruff format --check server/ && mypy server/app
python -m pytest server/tests/test_image_library_kind.py server/tests/test_image_library_inserter.py -q
```
Expected: 全 PASS。

- [ ] **Step 3: 提交**

```bash
git add server/app/modules/image_library/models.py \
  server/app/modules/image_library/router.py \
  server/alembic/versions/0043_stock_category_kind.py \
  server/tests/test_image_library_kind.py
git commit -m "feat(image-library): StockCategory 加 kind（主推/陪衬）+ ?kind= 过滤 + 迁移 0043

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 4: 开 PR**

```bash
git push -u origin feat/image-library-kind
gh pr create --fill --base main
```

---

# PR2 — `ai_illustrate` 配图节点（依赖 PR1）

> 开始前：`git checkout main && git pull && git checkout -b feat/ai-illustrate-node`（基于含 PR1 的 main）。

### Task 5: `category_contexts_for` helper

**Files:**
- Modify: `server/app/modules/articles/ai_format.py`（新增函数，紧挨现有 `all_category_contexts`）

- [ ] **Step 1: 写实现**

在 `all_category_contexts` 之后加：

```python
def category_contexts_for(
    db: Any,
    *,
    main_category_id: int,
    include_companion: bool = True,
) -> list[dict[str, Any]]:
    """配图节点候选栏目：主推栏目 + (可选)全部 kind=companion 栏目。

    返回 [{id,name,description}, ...]，主推排第一、去重。供 ai_illustrate 节点喂给
    run_ai_format 的 candidate_categories。
    """
    from server.app.modules.image_library.models import StockCategory

    result: list[dict[str, Any]] = []
    seen: set[int] = set()

    main = db.get(StockCategory, main_category_id)
    if main is not None:
        item = _category_context(main)
        if item is not None:
            result.append(item)
            seen.add(item["id"])

    if include_companion:
        companions = (
            db.query(StockCategory)
            .filter(StockCategory.kind == "companion")
            .order_by(StockCategory.id.asc())
            .all()
        )
        for cat in companions:
            item = _category_context(cat)
            if item is not None and item["id"] not in seen:
                result.append(item)
                seen.add(item["id"])

    return result
```

---

### Task 6: 写失败的节点测试

**Files:**
- Test: `server/tests/test_ai_illustrate_node.py`（Create）

- [ ] **Step 1: 写测试**

```python
import pytest

from server.tests.utils import build_test_app


def _make_category(app, name, bucket, kind):
    from server.app.modules.image_library.models import StockCategory

    with app.session_factory() as db:
        cat = StockCategory(name=name, bucket_name=bucket, kind=kind)
        db.add(cat)
        db.commit()
        db.refresh(cat)
        return cat.id


def _make_article(client):
    r = client.post(
        "/api/articles",
        json={
            "title": "配图测试",
            "content_json": {
                "type": "doc",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "这是正文段落"}]}
                ],
            },
            "content_html": "<p>这是正文段落</p>",
            "plain_text": "这是正文段落",
            "word_count": 5,
            "status": "draft",
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _uid(app):
    from server.app.modules.system.models import User

    with app.session_factory() as db:
        return db.query(User).first().id


@pytest.mark.mysql
def test_ai_illustrate_candidates_and_passthrough(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        main_id = _make_category(app, "主推A", "main-a", "main")
        comp_id = _make_category(app, "陪衬B", "comp-b", "companion")
        aid = _make_article(app.client)
        uid = _uid(app)

        captured: dict = {}

        def _stub(article_id, *, include_images, lock_started_at, preset_id, user_id, candidate_categories):
            captured["article_id"] = article_id
            captured["candidates"] = candidate_categories
            captured["include_images"] = include_images

        monkeypatch.setattr(
            "server.app.modules.pipelines.nodes.ai_illustrate.run_ai_format", _stub
        )

        from server.app.modules.pipelines.nodes.ai_illustrate import run_ai_illustrate
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        res = run_ai_illustrate(
            NodeRunContext(
                session_factory=app.session_factory,
                user_id=uid,
                config={"main_category_id": main_id, "include_companion": True},
                inputs={"article_ids": [aid]},
                upstream={},
            )
        )
        assert res.article_ids == [aid]
        assert captured["article_id"] == aid
        assert captured["include_images"] is True
        ids = {c["id"] for c in captured["candidates"]}
        assert main_id in ids and comp_id in ids
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_ai_illustrate_companion_toggle_off(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        main_id = _make_category(app, "主推A", "main-a", "main")
        comp_id = _make_category(app, "陪衬B", "comp-b", "companion")
        aid = _make_article(app.client)
        uid = _uid(app)
        captured: dict = {}
        monkeypatch.setattr(
            "server.app.modules.pipelines.nodes.ai_illustrate.run_ai_format",
            lambda article_id, **kw: captured.update(candidates=kw["candidate_categories"]),
        )
        from server.app.modules.pipelines.nodes.ai_illustrate import run_ai_illustrate
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        run_ai_illustrate(
            NodeRunContext(
                session_factory=app.session_factory,
                user_id=uid,
                config={"main_category_id": main_id, "include_companion": False},
                inputs={"article_ids": [aid]},
                upstream={},
            )
        )
        ids = {c["id"] for c in captured["candidates"]}
        assert ids == {main_id} and comp_id not in ids  # 关掉陪衬 → 只剩主推
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_ai_illustrate_empty_inputs(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        uid = _uid(app)
        from server.app.modules.pipelines.nodes.ai_illustrate import run_ai_illustrate
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        res = run_ai_illustrate(
            NodeRunContext(
                session_factory=app.session_factory,
                user_id=uid,
                config={"main_category_id": 1, "include_companion": True},
                inputs={"article_ids": []},
                upstream={},
            )
        )
        assert res.article_ids == []  # 无文章 → 安静跳过
    finally:
        app.cleanup()
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest server/tests/test_ai_illustrate_node.py -q`
Expected: FAIL（`ai_illustrate` 模块不存在 / ImportError）。

---

### Task 7: 实现节点

**Files:**
- Create: `server/app/modules/pipelines/nodes/ai_illustrate.py`
- Modify: `server/app/modules/pipelines/nodes/__init__.py`

- [ ] **Step 1: 写节点**

`server/app/modules/pipelines/nodes/ai_illustrate.py`：

```python
"""ai_illustrate 处理节点（前端「AI配图」）：给上游文章自动配图。

复用 articles.ai_format.run_ai_format：把「主推栏目 + (可选)全部陪衬栏目」作为候选栏目
喂给 AI格式 模型，由模型按文章内容决定插哪几张、插哪里（决策：主推+陪衬统一交 AI 决定）。
并发 max_workers=4，每篇独立置 ai_checking 锁（照 scheme_executor 成熟调用法）；
单篇失败收进 errors（partial_failed），不中断。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from server.app.core.time import utcnow
from server.app.modules.articles.ai_format import (
    category_contexts_for,
    has_ai_format_targets,
    run_ai_format,
)
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError


def run_ai_illustrate(ctx: NodeRunContext) -> NodeResult:
    cfg = ctx.config or {}
    article_ids = [a for a in (ctx.inputs.get("article_ids") or []) if isinstance(a, int)]
    if not article_ids:
        return NodeResult(
            output={"article_ids": [], "errors": [], "skipped": "无文章可配图"}, article_ids=[]
        )

    main_category_id = cfg.get("main_category_id")
    if not isinstance(main_category_id, int):
        raise ValidationError("ai_illustrate 节点需配置主推栏目 main_category_id")
    include_companion = bool(cfg.get("include_companion", True))
    cfg_preset_id = cfg.get("preset_id")

    errors: list[str] = []

    def _one(article_id: int) -> None:
        from server.app.modules.articles.models import Article
        from server.app.modules.system.models import User

        lock_started_at = utcnow().replace(microsecond=0)
        candidate_categories: list[Any] = []
        effective_preset: int | None = None

        db = ctx.session_factory()
        try:
            article = db.get(Article, article_id)
            if article is None or article.is_deleted:
                return
            if not has_ai_format_targets(article.content_json):
                return
            user = db.get(User, ctx.user_id)
            effective_preset = (
                cfg_preset_id
                if isinstance(cfg_preset_id, int)
                else (getattr(user, "ai_format_preset_id", None) if user else None)
            )
            candidate_categories = category_contexts_for(
                db, main_category_id=main_category_id, include_companion=include_companion
            )
            article.ai_checking = True
            article.ai_checking_started_at = lock_started_at
            article.ai_format_error = None
            db.commit()
        finally:
            db.close()

        run_ai_format(
            article_id,
            include_images=True,
            lock_started_at=lock_started_at,
            preset_id=effective_preset,
            user_id=ctx.user_id,
            candidate_categories=candidate_categories,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_one, aid): aid for aid in article_ids}
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:  # 单篇失败不中断，交由运行聚合为 partial_failed
                errors.append(f"article {futures[fut]}: {exc}")

    return NodeResult(
        output={"article_ids": article_ids, "errors": errors}, article_ids=article_ids
    )


register("ai_illustrate", run_ai_illustrate)
```

- [ ] **Step 2: 触发注册**

`server/app/modules/pipelines/nodes/__init__.py` 的 import 列表里加（保持字母序）：
```python
    ai_compose,  # noqa: F401
    ai_generate_node,  # noqa: F401
    ai_illustrate,  # noqa: F401
```

- [ ] **Step 3: 运行测试确认通过**

Run: `python -m pytest server/tests/test_ai_illustrate_node.py -q`
Expected: PASS（3 passed）。

- [ ] **Step 4: 确认节点出现在注册表**

Run:
```bash
python -c "import server.app.modules.pipelines.nodes; from server.app.modules.pipelines.nodes.base import registered_types; print('ai_illustrate' in registered_types())"
```
Expected: `True`。

---

### Task 8: PR2 收尾

- [ ] **Step 1: 后端门禁**

Run:
```bash
ruff check server/ && ruff format --check server/ && mypy server/app
python -m pytest server/tests/test_ai_illustrate_node.py -q
```
Expected: 全 PASS。

- [ ] **Step 2: 提交 + PR**

```bash
git add server/app/modules/articles/ai_format.py \
  server/app/modules/pipelines/nodes/ai_illustrate.py \
  server/app/modules/pipelines/nodes/__init__.py \
  server/tests/test_ai_illustrate_node.py
git commit -m "feat(pipelines): 新增 ai_illustrate 配图节点（复用 run_ai_format，主推+陪衬交 AI 配图）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
git push -u origin feat/ai-illustrate-node
gh pr create --fill --base main
```

---

# PR3 — 前端：图片库 tabs + 节点配置 UI（依赖 PR1）

> 开始前：`git checkout main && git pull && git checkout -b feat/image-library-frontend`。
> 前端无单测，门禁 = typecheck + build。每步实现后先 typecheck。

### Task 9: 类型 + API 加 `kind`

**Files:**
- Modify: `web/src/types.ts:381-388`
- Modify: `web/src/api/image-library.ts`

- [ ] **Step 1: types.ts**

`StockCategory` 类型加：
```typescript
  kind: "main" | "companion";
```

- [ ] **Step 2: image-library.ts — listCategories 支持 kind**

```typescript
export function listCategories(kind?: "main" | "companion"): Promise<StockCategory[]> {
  const qs = kind ? `?kind=${kind}` : "";
  return api<StockCategory[]>(`/api/image-library/categories${qs}`);
}
```

`createCategory` 的 payload 类型加可选 `kind?: "main" | "companion";`，并原样传入 body（已是整体 `JSON.stringify(payload)`，无需改函数体）。
`updateCategory` 的 payload 类型加可选 `kind?: "main" | "companion";`。

- [ ] **Step 3: typecheck**

Run: `pnpm --filter @geo/web typecheck`
Expected: 通过（此时 ImageLibraryWorkspace 可能因未用 kind 仍通过；若报未用错误，留到 Task 10 一起）。

---

### Task 10: 图片库 主推/陪衬 tabs

**Files:**
- Modify: `web/src/features/image-library/ImageLibraryWorkspace.tsx`

- [ ] **Step 1: 读现有组件**，找到加载栏目的 `listCategories()` 调用、栏目侧边栏渲染、以及新建栏目的提交处。

- [ ] **Step 2: 加 kind tab 状态与过滤**

- 顶部加两 tab：`主推游戏` / `陪衬游戏`，state `const [kindTab, setKindTab] = useState<"main"|"companion">("companion");`（默认陪衬，与「存量全陪衬」一致）。
- 加载栏目改为 `listCategories(kindTab)`；切 tab 重新加载并清空选中栏目。
- 新建栏目时带上 `kind: kindTab`（在当前 tab 下创建即归该类）。
- 视觉对齐 `demo.pen` 图片库帧（两 tab 在栏目列表上方）。沿用现有卡片/lightbox/骨架/空状态样式（见 `docs/specs/2026-05-26-image-library-ui-upgrade-design.md`）。

- [ ] **Step 3: typecheck + build**

Run: `pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 通过。

- [ ] **Step 4: 手动核对（dev server）**

Run: `pnpm --filter @geo/web dev`，进图片库：切「主推游戏/陪衬游戏」两 tab 各自只显示对应 kind 的栏目；在某 tab 下新建栏目后它出现在该 tab。

---

### Task 11: PipelineEditor「AI配图」节点

**Files:**
- Modify: `web/src/api/pipelines.ts`（节点类型/配置类型）
- Modify: `web/src/features/pipelines/PipelineEditor.tsx`（节点选项 + 配置面板）

- [ ] **Step 1: 读现有 PipelineEditor**，找到节点类型清单（如 `ai_compose` 的定义处）和其配置面板渲染的 switch/分支。

- [ ] **Step 2: 注册节点类型**

在节点类型清单里加 `ai_illustrate`（显示名「AI配图」，归「处理」类，与 `ai_compose` 同组）。`pipelines.ts` 里若有 node config 的联合类型，加：
```typescript
// ai_illustrate 节点配置
{ main_category_id?: number; include_companion?: boolean; preset_id?: number }
```

- [ ] **Step 3: 配置面板**（对应 demo Frame 1）

为 `ai_illustrate` 渲染：
- 主推下拉：`listCategories("main")` 拉取，选一个 → `config.main_category_id`。
- 「陪衬配图」开关：`config.include_companion`（默认 true）。
- 提示词模板下拉：复用现有 ai_format scope 模板下拉（如编辑器旧逻辑里有），选填 → `config.preset_id`。

- [ ] **Step 4: typecheck + build**

Run: `pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 通过。

- [ ] **Step 5: 手动核对**：在智能体管理里能添加「AI配图」节点，配置面板显示主推下拉（仅主推栏目）+ 陪衬开关 + 模板下拉；保存后刷新配置仍在。

---

### Task 12: PR3 收尾

- [ ] **Step 1: 门禁**

Run: `pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 通过。

- [ ] **Step 2: 提交 + PR**

```bash
git add web/src/types.ts web/src/api/image-library.ts web/src/api/pipelines.ts \
  web/src/features/image-library/ImageLibraryWorkspace.tsx \
  web/src/features/pipelines/PipelineEditor.tsx
git commit -m "feat(web): 图片库 主推/陪衬 tabs + 智能体管理「AI配图」节点配置

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
git push -u origin feat/image-library-frontend
gh pr create --fill --base main
```

---

# PR4 — 前端：编辑器去 AI格式/图片栏目 + 图片保存（依赖 PR1）

> 开始前：`git checkout main && git pull && git checkout -b feat/editor-image-save`。

### Task 13: 新增 `ImageSaveDialog` 组件

**Files:**
- Create: `web/src/components/editor/ImageSaveDialog.tsx`

- [ ] **Step 1: 写组件**（WPS 风格，对应 demo Frame 4/5）

要点（完整 props/行为，样式类沿用现有弹框风格）：
```typescript
import { useEffect, useState } from "react";
import { listCategories, uploadImage } from "../../api/image-library";
import type { StockCategory } from "../../types";

export function ImageSaveDialog({
  imageSrc,
  onClose,
  onSaved,
}: {
  imageSrc: string;          // editor.getAttributes("image").src
  onClose: () => void;
  onSaved: (msg: string) => void;
}) {
  const [kind, setKind] = useState<"main" | "companion">("companion");
  const [cats, setCats] = useState<StockCategory[]>([]);
  const [categoryId, setCategoryId] = useState<number | null>(null);
  const [filename, setFilename] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setCategoryId(null);
    listCategories(kind).then(setCats).catch(() => setCats([]));
  }, [kind]);

  async function handleSave() {
    if (categoryId == null) { setError("请选择栏目"); return; }
    setSaving(true);
    setError(null);
    try {
      const resp = await fetch(imageSrc);
      if (!resp.ok) throw new Error("读取图片失败");
      const blob = await resp.blob();
      const type = blob.type || "image/png";
      const ext = type.split("/")[1] || "png";
      const name = (filename.trim() || `image-${Date.now()}`) + (filename.includes(".") ? "" : `.${ext}`);
      const file = new File([blob], name, { type });
      await uploadImage({ category_id: categoryId, file });
      onSaved(`已保存到图库：${name}`);
      onClose();
    } catch (e) {
      // 跨源图片 fetch 会被 CORS 挡 → 提示走兜底（PR4 可选 from-url）或先下载再上传
      setError(e instanceof Error ? e.message : "保存失败（可能是跨源图片）");
    } finally {
      setSaving(false);
    }
  }

  return (
    /* 三段：① 主推/陪衬 tab（setKind）② 栏目列表（cats，选 categoryId）
       ③ 文件名输入 + 保存/取消按钮；error 显示在底部。布局参照 demo Frame 4/5。 */
    null as any
  );
}
```
> JSX 主体按 demo Frame 4/5 实现（kind 两 tab → 栏目单选列表 → 文件名输入 → 取消/保存）。`Date.now()` 在前端可用（仅 workflow 脚本禁用，普通 React 代码不受限）。

- [ ] **Step 2: typecheck**

Run: `pnpm --filter @geo/web typecheck`
Expected: 通过（补完 JSX 后）。

---

### Task 14: EditorToolbar 去 AI格式、加图片保存

**Files:**
- Modify: `web/src/components/editor/EditorToolbar.tsx:9-23,85-96`

- [ ] **Step 1: 改 props**

删 `aiChecking` / `aiFormatRemainingSeconds` / `onAiFormat` / `stockCategorySelected`，新增：
```typescript
  imageSelected: boolean;     // editor.isActive("image")
  onSaveImage: () => void;    // 打开 ImageSaveDialog
```

- [ ] **Step 2: 替换按钮（L85-96）**

```tsx
      <span className="toolbarSep" />

      <button
        onClick={onSaveImage}
        disabled={!imageSelected}
        title={imageSelected ? "把选中图片存进图库" : "先选中正文中的图片"}
        type="button"
      >
        图片保存
      </button>
```

- [ ] **Step 3: typecheck**（此时 ContentWorkspace 调用处会报错，下一 Task 修）

Run: `pnpm --filter @geo/web typecheck`
Expected: 在 ContentWorkspace 处报 props 不匹配 —— 预期内，Task 15 修复。

---

### Task 15: ContentWorkspace 去图片栏目 + 接保存框

**Files:**
- Modify: `web/src/features/content/ContentWorkspace.tsx`

- [ ] **Step 1: 读现有组件**，定位：① `<EditorToolbar .../>` 调用处及其传入的 `onAiFormat/aiChecking/...`；② AI格式 触发逻辑（调 `/api/articles/{id}/ai-format` 的函数）；③「图片栏目」选择 UI 与其 state/请求。

- [ ] **Step 2: 删除**

- 删「图片栏目」选择 UI 及其 state、`listCategories` 用于该选择的逻辑、对 `article.stock_categories` 的写入。
- 删 AI格式 触发函数与其轮询状态（`aiChecking` 等）。
- `<EditorToolbar>` 调用改为新 props：
```tsx
<EditorToolbar
  editor={editor}
  onImageUpload={handleImageUpload}
  imageSelected={!!editor?.isActive("image")}
  onSaveImage={() => {
    const src = editor?.getAttributes("image").src as string | undefined;
    if (src) setSaveImageSrc(src);
  }}
/>
```

- [ ] **Step 3: 接入弹框**

```tsx
const [saveImageSrc, setSaveImageSrc] = useState<string | null>(null);
// ...
{saveImageSrc && (
  <ImageSaveDialog
    imageSrc={saveImageSrc}
    onClose={() => setSaveImageSrc(null)}
    onSaved={(msg) => {/* 复用现有 toast/提示机制 */}}
  />
)}
```

- [ ] **Step 4: typecheck + build**

Run: `pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 通过。

- [ ] **Step 5: 手动核对**：编辑器工具栏 AI格式 没了、图片栏目选择没了；选中正文图片后「图片保存」可点 → 弹框选 主推/陪衬→栏目→文件名→保存 → 图片出现在图片库对应栏目。

---

### Task 16:（可选）`from-url` 兜底端点

> 仅当 Task 15 手测发现跨源图片 `fetch` 被 CORS 挡才做。

**Files:**
- Modify: `server/app/modules/image_library/router.py`
- Modify: `web/src/components/editor/ImageSaveDialog.tsx`（fetch 失败时回退调 from-url）

- [ ] **Step 1: 后端端点**：`POST /api/image-library/images/from-url`，body `{url, category_id, filename, tags?, description?}`，服务端用 `httpx`（仓库已有依赖则复用）抓取 url → 复用 `upload_image` 的存储逻辑创建 `StockImage`；抓取失败返回 400。加审计 `stock_image.create`。
- [ ] **Step 2: 测试**：mock 抓取，验证成功创建 + 非法 url 400。
- [ ] **Step 3: 前端**：`ImageSaveDialog.handleSave` 在 `fetch(imageSrc)` 抛错时回退调 from-url。
- [ ] **Step 4: 门禁**：后端 pytest + 前端 typecheck/build。

---

### Task 17: PR4 收尾

- [ ] **Step 1: 门禁**

Run: `pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
（若做了 Task 16：另跑 `python -m pytest server/tests/test_image_library_kind.py server/tests/<新 from-url 测试> -q` + `ruff`/`mypy`。）
Expected: 通过。

- [ ] **Step 2: 提交 + PR**

```bash
git add web/src/components/editor/EditorToolbar.tsx \
  web/src/components/editor/ImageSaveDialog.tsx \
  web/src/features/content/ContentWorkspace.tsx
# 若做了 Task 16，另 add server/app/modules/image_library/router.py 与新测试
git commit -m "feat(editor): 去掉 AI格式/图片栏目，新增图片保存（WPS 风格保存到图库）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
git push -u origin feat/editor-image-save
gh pr create --fill --base main
```

---

## Self-Review（计划自检）

- **Spec 覆盖**：① kind 列/迁移 → PR1；② `?kind=` 过滤 → PR1 Task 2 Step 5；③ ai_illustrate 节点 + candidate_categories → PR2；④ 主推+陪衬交 AI（决策B）→ `category_contexts_for` + 节点 include_images=True；⑤ 图片库两 tab → PR3 Task 10；⑥ 节点配置面板 → PR3 Task 11；⑦ 编辑器去 AI格式/图片栏目 → PR4 Task 14/15；⑧ 图片保存 WPS 框 → PR4 Task 13/15；⑨ from-url 兜底 → PR4 Task 16（可选）；⑩ M2M 休眠 → 不写删除迁移（无任务即正确）。全覆盖。
- **占位符**：无 TBD/TODO；前端 JSX 主体明确指向 demo Frame 4/5 并给出完整逻辑骨架（净新增文件给全代码，既有大组件给锚点 + 片段，符合"既有代码先读再改"）。
- **类型一致**：`kind` 值统一 `"main"|"companion"`；节点函数名 `run_ai_illustrate`、注册名 `ai_illustrate`、helper `category_contexts_for` 在 PR2 各处一致；`run_ai_format` 入参（`include_images/lock_started_at/preset_id/user_id/candidate_categories`）与 [ai_format.py:541](../../server/app/modules/articles/ai_format.py) 签名一致。

---

## 执行顺序

PR1 →（合并后）PR2 / PR3 可并行 → PR4。PR3/PR4 都依赖 PR1 的 `kind`；PR4 的节点配置不依赖 PR3，但 PR3 先合并能让节点端到端可用。
