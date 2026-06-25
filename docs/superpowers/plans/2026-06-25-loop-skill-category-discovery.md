# `list_stock_categories` MCP 工具 + onboarding 自助化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消掉 /goal Loop onboarding step 5「自己开 GEO 后台手抄 main_category_id」的 friction — 让 Claude 调 `list_stock_categories` MCP 工具查候选展示给用户，用户选 id 后 Claude 用 Edit 工具改本机 SKILL.md。

**Architecture:** 在 `mcp_catalog_router` 加只读 endpoint `GET /api/mcp/stock-categories`（join StockCategory + COUNT StockImage 一次性算 image_count）；`catalog.py` 加薄壳 MCP 工具；writer skill 矩阵段加 1 句提示、README onboarding step 5 重写；bump bundle v2→v3。

**Tech Stack:** FastAPI + Pydantic v2 / FastMCP / SQLAlchemy 2.x (outerjoin + group_by 子查询避免 N+1) / pytest `@pytest.mark.mysql`

**Spec:** [`docs/superpowers/specs/2026-06-25-loop-skill-category-discovery-design.md`](../specs/2026-06-25-loop-skill-category-discovery-design.md)

**Branch:** `fix/loop-skill-category-discovery`（已从 `origin/main` (12f0bae) 拉出，spec 已 commit 在 `7352679`）

---

## Files to Touch

| 文件 | 操作 | 责任 |
|---|---|---|
| `server/app/modules/mcp_catalog/router.py` | 修改 | 追加 `StockCategoryRead` Pydantic 模型 + `GET /stock-categories` endpoint |
| `server/mcp/tools/catalog.py` | 修改 | 追加 `list_stock_categories` async 工具 |
| `server/app/modules/mcp_catalog/connect_router.py` | 修改 | `MCP_TOOLS_COUNT` 20→21 |
| `server/tests/test_mcp_connect.py` | 修改 | `tools_count == 20` 断言改成 21 |
| `server/tests/test_mcp_stock_categories.py` | 新建 | 5 个集成测试 |
| `server/app/modules/loop_skills/templates/skills/geo-article-writer/SKILL.md` | 修改 | 矩阵段加 1 句「不知道 id 就问 Claude」 |
| `server/app/modules/loop_skills/templates/README.md` | 修改 | onboarding step 5 重写为「让 Claude 帮你查 + 填」 |
| `server/app/modules/loop_skills/version.py` | 修改 | bump `LOOP_SKILL_BUNDLE_VERSION` v2→v3，KNOWN_BUNDLE_SHAS 加新 sha（v1/v2 保留） |

**关键边界**：
- endpoint 是只读 list 查询，无业务异常、无写操作
- MCP 工具是 `_aget` 薄壳，不在工具层做过滤/排序/格式化（endpoint 做完）
- skill / README 改动会 bump bundle sha → 必须 bump version + 加 sha 到 KNOWN
- 不动 `image_library/router.py`（user JWT 端点保留）、不动 StockCategory 模型

---

## Task 1: endpoint `GET /api/mcp/stock-categories` + 5 集成测试（TDD）

**Files:**
- Modify: `server/app/modules/mcp_catalog/router.py`（追加）
- Test: `server/tests/test_mcp_stock_categories.py`（新建）

- [ ] **Step 1: 写 5 个失败的测试**

创建 `server/tests/test_mcp_stock_categories.py`：

```python
"""GET /api/mcp/stock-categories MCP endpoint 测试.

5 用例覆盖：鉴权 / 不带 kind 返全量 / kind 过滤 / image_count 正确性 / 排序.
"""

from __future__ import annotations

import pytest

from server.tests.utils import build_test_app


def _mk_category(test_app, *, name: str, kind: str = "main", description: str | None = None,
                  official_url: str | None = None) -> int:
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

        _mk_category(test_app, name="餐厅养成记", kind="main", description="餐厅经营",
                     official_url="https://example.com/restaurant")
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
        # main 字典序 < companion，所以应该是 [main, main, companion, companion]
        assert kinds == ["main", "main", "companion", "companion"]
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 跑测试，确认 5 个全 fail**

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_mcp_stock_categories.py -q
```

**预期**：5 个测试 fail，原因：endpoint 还没注册 → 401/404/500。具体取决于：test_endpoint_requires_mcp_token 可能直接 401（因为路径不存在 + middleware 拦截 None）—— 这其实是 pass 的情况；其它 4 个会 404 因为 endpoint 未注册。可能 1 pass / 4 fail，也可能 5 fail。重要的是接下来 Step 4 跑完要 **5 pass**。

- [ ] **Step 3: 在 router.py 追加 Pydantic + endpoint**

修改 `server/app/modules/mcp_catalog/router.py`：

(a) 顶部 import 区追加（如果还没 import）：

```python
from pydantic import BaseModel

from server.app.modules.image_library.models import StockCategory, StockImage
```

> 先 Read router.py 顶部确认 BaseModel / StockCategory / StockImage 是否已 import；按需补全。

(b) 在文件末尾追加：

```python
# ── stock-categories ───────────────────────────────────────────────────────


class StockCategoryRead(BaseModel):
    id: int
    name: str
    kind: str
    description: str | None
    official_url: str | None
    image_count: int


@router.get("/stock-categories", response_model=list[StockCategoryRead])
def mcp_list_stock_categories(
    kind: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[StockCategoryRead]:
    """[MCP] 列图库栏目（service 视角，无 per-user 过滤）.

    给 /goal Loop onboarding 用——使用者填 main_category_id 前先让 Claude
    调本工具看候选栏目. kind 过滤：'main' / 'companion' / None=全量.
    """
    # COUNT(StockImage) per category via subquery —— 一次 SQL 避免 N+1
    count_subq = (
        select(StockImage.category_id, func.count(StockImage.id).label("cnt"))
        .group_by(StockImage.category_id)
        .subquery()
    )
    q = (
        db.query(StockCategory, func.coalesce(count_subq.c.cnt, 0).label("image_count"))
        .outerjoin(count_subq, count_subq.c.category_id == StockCategory.id)
        .order_by(StockCategory.kind.asc(), StockCategory.id.asc())
    )
    if kind:
        q = q.filter(StockCategory.kind == kind)

    rows = q.all()
    return [
        StockCategoryRead(
            id=cat.id,
            name=cat.name,
            kind=cat.kind,
            description=cat.description,
            official_url=cat.official_url,
            image_count=int(image_count),
        )
        for cat, image_count in rows
    ]
```

- [ ] **Step 4: 跑测试，确认 5 个全 pass**

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_mcp_stock_categories.py -q
```

**预期**：`5 passed`。

- [ ] **Step 5: ruff + format clean**

```bash
docker compose exec app ruff check server/app/modules/mcp_catalog/router.py server/tests/test_mcp_stock_categories.py
docker compose exec app ruff format --check server/app/modules/mcp_catalog/router.py server/tests/test_mcp_stock_categories.py
```

如 format 报差异，去掉 `--check` 直接改写。

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/mcp_catalog/router.py server/tests/test_mcp_stock_categories.py
git commit -m "$(cat <<'EOF'
feat(mcp_catalog): GET /api/mcp/stock-categories endpoint + 5 集成测试

给 /goal Loop onboarding 用：使用者填 main_category_id 前 Claude 调这个
endpoint 看候选栏目（id / name / kind / description / official_url +
COUNT(StockImage) 一次性算）。outerjoin + group_by 子查询避免 N+1，
空栏目 image_count=0 不藏；order_by kind asc 让 main 排 companion 前。
StockCategoryRead Pydantic 模型扁平放在 router.py（只这一处用）。

5 个 mysql 测试覆盖：401 鉴权 / 全量返 / kind 过滤 / image_count 正确
（含空栏目 0）/ 排序。Task 2 会加 MCP 工具薄壳调本端点。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: MCP 工具 `list_stock_categories` 薄壳 + MCP_TOOLS_COUNT 20→21

**Files:**
- Modify: `server/mcp/tools/catalog.py`（末尾追加）
- Modify: `server/app/modules/mcp_catalog/connect_router.py`（MCP_TOOLS_COUNT 20→21）
- Modify: `server/tests/test_mcp_connect.py`（断言 20→21）

工具无新自动测——薄壳 `_aget` 包装，端到端鉴权 + 行为已被 Task 1 的 endpoint 测试覆盖。

- [ ] **Step 1: 在 catalog.py 末尾追加新工具**

```python
@mcp.tool()
async def list_stock_categories(
    kind: str | None = None,
) -> dict[str, Any]:
    """List stock image library categories (image buckets) — for /goal Loop onboarding.

    Use this when the user needs to find a `main_category_id` to fill into
    their writer SKILL.md matrix section. Show the returned list to the user
    so they can pick the one matching their content matrix (e.g. "餐厅养成记").

    Args:
        kind: Filter by category kind. Common values:
            - "main": 主推栏目 (one per content matrix — what writer skill picks)
            - "companion": 陪衬栏目 (AI auto-detects across all of them)
            - None (default): return all categories

    Returns:
        {"ok": True, "data": [
            {
                "id": int,
                "name": str,           # e.g. "餐厅养成记"
                "kind": str,           # "main" | "companion"
                "description": str | None,
                "official_url": str | None,
                "image_count": int,    # total images in this bucket
            },
            ...
        ], "error": None}
    """
    params: dict[str, Any] = {}
    if kind:
        params["kind"] = kind
    return await _aget("/api/mcp/stock-categories", params=params or None)
```

- [ ] **Step 2: ruff + import 自检**

```bash
docker compose exec app ruff check server/mcp/tools/catalog.py
docker compose exec app ruff format --check server/mcp/tools/catalog.py
docker compose exec app python -c "import server.mcp.tools.catalog; print('ok')"
```

**预期**：`ok`。

- [ ] **Step 3: 确认 MCP 工具数 +1**

```bash
docker compose exec app python -c "from server.mcp.server import mcp; print('tools count:', len(mcp._tool_manager._tools))"
```

**预期**：原来 20 → 现在 **21**。

- [ ] **Step 4: 同步 MCP_TOOLS_COUNT 到 21**

修改 `server/app/modules/mcp_catalog/connect_router.py`：

把 `MCP_TOOLS_COUNT = 20` 改成 `MCP_TOOLS_COUNT = 21`。

- [ ] **Step 5: 同步 test_mcp_connect 断言**

修改 `server/tests/test_mcp_connect.py`，找到 `test_status_returns_configured_true_when_token_set` 那个断言：

```python
        assert body["tools_count"] == 20
```

改成：

```python
        assert body["tools_count"] == 21
```

- [ ] **Step 6: 跑测试确认 test_mcp_connect 全过**

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_mcp_connect.py -q
```

**预期**：全 pass（原来 7 个，断言变更后仍 7 pass）。

- [ ] **Step 7: ruff + format clean**

```bash
docker compose exec app ruff check server/mcp/tools/catalog.py server/app/modules/mcp_catalog/connect_router.py server/tests/test_mcp_connect.py
docker compose exec app ruff format --check server/mcp/tools/catalog.py server/app/modules/mcp_catalog/connect_router.py server/tests/test_mcp_connect.py
```

- [ ] **Step 8: Commit**

```bash
git add server/mcp/tools/catalog.py server/app/modules/mcp_catalog/connect_router.py server/tests/test_mcp_connect.py
git commit -m "$(cat <<'EOF'
feat(mcp): list_stock_categories 工具 + MCP_TOOLS_COUNT 20→21 同步

catalog 组从 9 个工具增到 10 个。async + _aget 薄壳，转发到后端
/api/mcp/stock-categories（Task 1 加的）；单可选参数 kind 过滤
（main / companion / None=全量）。docstring 明确告诉 Claude 这是 onboarding
场景用的——查 main_category_id 候选给用户选。

MCP_TOOLS_COUNT 20→21 同步 + test_mcp_connect 断言同步。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: writer skill + README 文案改

**Files:**
- Modify: `server/app/modules/loop_skills/templates/skills/geo-article-writer/SKILL.md`
- Modify: `server/app/modules/loop_skills/templates/README.md`

- [ ] **Step 1: 改 writer skill 矩阵特例段**

**先 Read** `server/app/modules/loop_skills/templates/skills/geo-article-writer/SKILL.md` 找到「矩阵特例」段下面的第 3 个 bullet（配图主推栏目那行）。当前内容（PR #149 后）：

```markdown
- 配图主推栏目：`main_category_id = <REPLACE_ME>`  # ← 安装时查 GEO 后台
  「图库管理」→ 主推栏目「餐厅养成记」的 id；写死在这里
```

用 Edit 替换为：

```markdown
- 配图主推栏目：`main_category_id = <REPLACE_ME>`  # ← 安装时填，**不知道 id 就
  对 Claude 说「帮我查下主推栏目，我用<矩阵名>」**，它会调 list_stock_categories
  MCP 工具列候选并用 Edit 工具帮你写到这里。也可以去 GEO 后台「图库管理」→
  主推栏目手抄 id
```

**注意**：只替换这一个 bullet，不动「## 加新矩阵」段、不动「配图风格 / 封面 / 陪衬」3 个 bullet。

- [ ] **Step 2: 改 README onboarding step 5**

**先 Read** `server/app/modules/loop_skills/templates/README.md` 找到 step 5（PR #149 后的内容）。当前内容：

```
5. 打开本机 .claude/skills/geo-article-writer/SKILL.md，找到「矩阵特例」段
   `main_category_id = <REPLACE_ME>` 行；去 GEO 后台「图库管理」→ 主推栏目
   里找你矩阵对应栏目（比如餐厅养成记），把 id 填进去（数字）。
```

用 Edit 替换为：

```
5. 让 Claude 帮你填 main_category_id：
   在 Claude Code 主对话里直接说：
     "帮我查下主推栏目，我用的是<你的矩阵名>"
   Claude 会调 list_stock_categories MCP 工具列出候选（含 id / 名称 / 图数），
   你选一个 id 告诉它，Claude 会用 Edit 工具帮你写到本机
   .claude/skills/geo-article-writer/SKILL.md 的「矩阵特例」段。

   也可以走老路：自己去 GEO 后台「图库管理」→ 主推栏目查 id，手动 vim 改文件。
```

**注意**：step 1-4 / 6 都不动；标题 `## 第一次用 /goal —— 6 步 onboarding` 不动。

- [ ] **Step 3: 跑 sanity check 确认改对**

```bash
docker compose exec app python -c "
from pathlib import Path
tpl = Path('server/app/modules/loop_skills/templates')

writer = (tpl / 'skills/geo-article-writer/SKILL.md').read_text(encoding='utf-8')
assert 'list_stock_categories' in writer, 'writer skill missing list_stock_categories mention'
assert 'main_category_id = <REPLACE_ME>' in writer, 'writer skill REPLACE_ME placeholder gone'
assert 'main_category_id = <REPLACE_ME>  # ← 安装时填' in writer, 'writer matrix bullet not updated to new prompt-Claude form'

readme = (tpl / 'README.md').read_text(encoding='utf-8')
assert 'list_stock_categories' in readme, 'README step 5 missing list_stock_categories mention'
assert '让 Claude 帮你填 main_category_id' in readme, 'README step 5 not rewritten'
assert '6 步 onboarding' in readme, 'README step count title regressed'

print('all template sanity checks passed')
"
```

**预期**：`all template sanity checks passed`。

- [ ] **Step 4: Commit**

```bash
git add server/app/modules/loop_skills/templates/
git commit -m "$(cat <<'EOF'
docs(loop_skills/templates): writer 矩阵段 + README onboarding step 5 改为 Claude 自助填

PR #149 留下的 onboarding step 5「自己开 GEO 后台手抄 main_category_id」
体验差。改成让用户直接对 Claude 说「帮我查下主推栏目」，Claude 调
list_stock_categories MCP 工具（Task 2 加的）列候选展示，用户选 id 后
Claude 用 Edit 工具帮写到本机 SKILL.md。

writer 矩阵段第 3 个 bullet 加 1 句提示；README step 5 整段重写，老路
（去 GEO 后台手抄）保留作 fallback 一句。step 1-4 / 6 不动。

下一步 Task 4 bump bundle v2→v3，让分发链路把模板更新推到所有使用者本机。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: bundle version bump v2→v3 + 新 sha 加 KNOWN

**Files:**
- Modify: `server/app/modules/loop_skills/version.py`

- [ ] **Step 1: 跑 build_bundle 拿当前 sha**

```bash
docker compose exec app python -c "from server.app.modules.loop_skills.service import build_bundle; print(build_bundle().bundle_sha256)"
```

**预期**：打出一串 64 字符 hex（v3 sha，区别于 v1 的 `49f824a3...` 和 v2 的 `abd8416c...`）。**复制这串**到下一步。

- [ ] **Step 2: 跑 sha 校验测试，确认 fail**

```bash
docker compose exec app pytest server/tests/test_loop_skill_bundle.py::test_bundle_sha_is_known -v
```

**预期**：fail，错误信息会打出 `Bundle sha256 = '<step 1 拿到的 sha>' not in KNOWN_BUNDLE_SHAS`。

- [ ] **Step 3: bump version + 加 sha 到 KNOWN**

修改 `server/app/modules/loop_skills/version.py`：

```python
"""手工维护的 bundle 版本号 + 已审核 sha 集合.

CI 测试 (test_loop_skill_bundle.py::test_bundle_sha_is_known) 会校验：
如果 build_bundle().bundle_sha256 没记录在 KNOWN_BUNDLE_SHAS 集合里，
fail + 提示开发者：把新 sha 加进 KNOWN_BUNDLE_SHAS 并 bump
LOOP_SKILL_BUNDLE_VERSION，强制「改模板必同步 bump 版本」纪律.
"""

LOOP_SKILL_BUNDLE_VERSION = "2026-06-25-v3"

KNOWN_BUNDLE_SHAS: frozenset[str] = frozenset(
    {
        # v1 (2026-06-24)
        "49f824a36606c285b84c71ade5aec406ea3c545599b7a3ccf59f863b521667dd",
        # v2 (2026-06-25): writer step 5 + 矩阵特例 + orchestrator 日志中文化
        "abd8416c51f0b591c85cee0c3635645a10a313a2cedbeb52b89953a2c41e7fea",
        # v3 (2026-06-25): writer 矩阵段 + README step 5 改为 list_stock_categories 自助路径
        "<把 step 1 打出的 sha 串填到这里，64 字符 hex>",
    }
)
```

把 `<把 step 1 打出的 sha 串填到这里>` 替换为 Step 1 实际打印出的 sha。

- [ ] **Step 4: 跑 bundle 全部测试，确认仍全 pass**

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_loop_skill_bundle.py -q
```

**预期**：`9 passed`（其中 `test_bundle_sha_is_known` 现在通过）。

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/loop_skills/version.py
git commit -m "$(cat <<'EOF'
chore(loop_skills): bump LOOP_SKILL_BUNDLE_VERSION v2→v3 + 加 v3 sha 到 KNOWN

Task 3 改了 writer 矩阵段 + README step 5 模板，bundle sha 自然变.
v1 / v2 sha 保留：已经装了旧版的使用者本机 .claude/ 里就是旧版内容；
他们升级前 KNOWN 仍要认这些 sha. Web Section ⑤ 的 /info 端点会返回
当前 v3 + 新 sha，使用者比对自己本机版本判断是否要重装.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 全 lint/test + push + PR

不引入新文件；本任务是集成验证。

- [ ] **Step 1: 后端硬门禁**

```bash
docker compose exec app ruff check server/
docker compose exec app ruff format --check server/
docker compose exec app mypy server/app
```

**预期**：0 error。mypy 不可用就跳（CI 上会跑）。

- [ ] **Step 2: 后端全部测试**

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/ -q
```

**预期**：全 pass。本次新增 5 个测试 + 1 个断言更新（test_mcp_connect 20→21）+ 1 个 sha 加 KNOWN。

- [ ] **Step 3: 前端 typecheck（前端零改动应自然过）**

```bash
pnpm --filter @geo/web typecheck
```

**预期**：0 error。

- [ ] **Step 4: 推分支**

```bash
git push -u origin fix/loop-skill-category-discovery
```

- [ ] **Step 5: 建 PR**

```bash
gh pr create --title "feat(mcp): list_stock_categories 工具 — /goal Loop onboarding 自助填 main_category_id" --body "$(cat <<'EOF'
## Summary

消掉 [PR #147](https://github.com/geo-ihuanlegame/geo-collab/pull/147) + [PR #149](https://github.com/geo-ihuanlegame/geo-collab/pull/149) 留下的 onboarding step 5 friction：现状要使用者切到 GEO 后台手抄 \`main_category_id\`，新方案让 Claude 调 \`list_stock_categories\` MCP 工具列候选（含 id / 名称 / 图数）展示给用户，用户选 id 后 Claude 用 Edit 工具帮写到本机 SKILL.md。

- 新增 endpoint \`GET /api/mcp/stock-categories\`（mcp_catalog router，require_mcp_token）
- 新增 MCP 工具 \`list_stock_categories(kind=None)\` 薄壳
- MCP_TOOLS_COUNT 20→21
- writer skill 矩阵段加 1 句「不知道 id 就问 Claude」
- README onboarding step 5 整段重写（老路保留作 fallback）
- bump bundle v2→v3，v1/v2 sha 保留兼容未升级用户

## Test plan

- [x] 后端 ruff / format / pytest 全过（CI 门禁）
- [x] 5 个新单测通过（401 鉴权 / 全量返 / kind 过滤 / image_count 正确性 / 排序）
- [x] test_mcp_connect 工具数断言 20→21 同步
- [x] test_bundle_sha_is_known v3 sha 加 KNOWN 通过
- [x] 前端 typecheck 通过（前端零改动）
- [ ] **使用者本地装 v3 + 跑 \`帮我查下主推栏目，我用的是<矩阵名>\`**：Claude 调 list_stock_categories 展示候选，告诉 id 后 Claude 用 Edit 改本机 SKILL.md（user 手动验证）
- [ ] **改完跑 \`/goal 1 篇国风游戏文章作为冒烟\`**：走通 + 文章有插图 + 有封面（user 手动验证）

## 设计 / 实施

- 设计稿：\`docs/superpowers/specs/2026-06-25-loop-skill-category-discovery-design.md\`
- 实施 plan：\`docs/superpowers/plans/2026-06-25-loop-skill-category-discovery.md\`
- 上游 PR：#147 (已合) + #149 (已合)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

如果 gh 命令失败（认证 / 仓库权限），记录命令 + 错误给 user 手动跑。

---

## Self-Review

**1. Spec coverage check** — 每节是否有对应 task？

| Spec 节 | Task 覆盖 |
|---|---|
| §0 一句话定位 | Plan Goal 段引用 |
| §1 问题定位 | 整体 Task 1-3 解决 |
| §2 锁定决策 6 项 | 全部体现在 Task 1-3 实施细节 |
| §3 架构总览 + 文件清单 | Plan Files to Touch 表 + Task 顺序匹配 §9.2 |
| §4.1 StockCategoryRead schema | Task 1 Step 3 |
| §4.2 endpoint 完整代码 | Task 1 Step 3 |
| §4.3 MCP 工具签名 | Task 2 Step 1 |
| §5.1 writer skill 矩阵段 | Task 3 Step 1 |
| §5.2 README onboarding step 5 | Task 3 Step 2 |
| §6 version bump | Task 4 |
| §7 失败矩阵 + 不变式 | Task 1 的 5 个测试覆盖关键失败场景；endpoint 设计本身实现不变式 |
| §8.1 自动测 5 用例 | Task 1 全包 |
| §8.2 手工冒烟 4 步 | Task 5 PR description 里 Test plan 列出（unchecked 由 user 验证） |
| §9 工作量 + 顺序 | Plan task 顺序就是 §9.2 |
| §10 与已合 PR 关系 | Plan Architecture + Branch 段引用 |
| §11 Out of Scope | 隐含落实 |
| §12 上线门禁 | Task 5 PR description Test plan |

**结论：全覆盖**。

**2. Placeholder scan** — 检查无 TBD / TODO：
- Task 4 Step 3 的 `<把 step 1 打出的 sha 串填到这里>` 是**有意运行时占位**（必须实际跑 build_bundle 拿到 sha 才知道），文档清楚说明替换流程 ✓
- 其它无遗留占位

**3. Type consistency**
- `StockCategoryRead(id, name, kind, description, official_url, image_count)` — Task 1 定义、Task 1 tests 断言字段集合、Task 2 工具 docstring 描述返回字段，命名一致
- `list_stock_categories(kind: str | None = None)` — Task 2 工具签名、Task 1 endpoint 接受 kind 参数（值 'main' / 'companion' / None），命名一致
- `MCP_TOOLS_COUNT = 21` — Task 2
- `LOOP_SKILL_BUNDLE_VERSION = "2026-06-25-v3"` — Task 4
- `<REPLACE_ME>` 占位符 — Task 3 在 writer skill 中保留（设计不变 v2 留下的占位）

**结论：一致**。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-25-loop-skill-category-discovery.md`. Two execution options:

**1. Subagent-Driven (recommended)** — 我每个 task 起 fresh subagent 跑，task 之间 review，迭代快。

**2. Inline Execution** — 我在当前会话里逐 task 顺序执行，每 2-3 个 task 检查一次。

Which approach?
