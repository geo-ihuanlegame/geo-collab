# 方案生文自动排版/配图 + Skill 管理下线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 方案运行生文成功后自动跑 AI 排版（标题识别 + 用全部图片 bucket 智能配图）；同时把 AI生文 tab 里的「技能与提示词」删掉、`/api/skills` 下线（提示词管理已在「提示词管理」tab 覆盖 generation + ai_format 两种 scope）。

**Architecture:** 需求 2（自动排版/配图）落在 `ai_format.py` 加一个可选 `candidate_categories` 入口（不传 = 现状行为，传 = 用全部 bucket 当候选），由 `scheme_executor._execute_task` 在生文成功后 best-effort 调用，候选栏目来自 `all_category_contexts(db)`（全部 `StockCategory`）。需求 1（删 Skill）= 前端删「技能与提示词」子 tab + skill API + Skill 类型，后端 unmount `/api/skills`；`skills` 表 / `GenerationSession.skill_id` / skills 模块文件全部**保留休眠、不删不迁移**。两需求互不依赖。

**Tech Stack:** FastAPI + SQLAlchemy（MySQL only）、Pydantic v2、LiteLLM、React 19 + Vite + TS、pytest（`@pytest.mark.mysql` + `build_test_app`）。

**分支约定（已更新）：** 原在 `feat/question-scheme-pool` 上开发，**已通过 PR #13 合入 `main`**（本地门禁 + 真实 E2E 灰度验证通过，2026-06）。

**决策（已与用户确认）：**
1. `skills` 表 / `skill_id` 列 **留休眠**，不 drop、不写迁移。
2. 自动 AI 排版用 **该 run 发起用户的 `User.ai_format_preset_id`**（无则走内置兜底提示词）。
3. 命中 bucket 后 **只插图，不把类别写回文章**（不为追溯写 `article.stock_categories`）。

---

## File Structure

需求 2（后端，先做——是实质逻辑）：
- Modify `server/app/modules/articles/ai_format.py` — 新增 `all_category_contexts(db)`；`_maybe_insert_images` 加可选 `available_categories` kwarg；`run_ai_format` 加 `candidate_categories` 参数并透传。
- Modify `server/app/modules/ai_generation/scheme_executor.py` — 新增 `_auto_format_article(...)`；在 `_execute_task` 标 task done 后 best-effort 调用。
- Test `server/tests/test_ai_format.py` — `all_category_contexts` + `candidate_categories` 路径。
- Test `server/tests/test_scheme_autoformat.py`（新建） — `_auto_format_article` 设锁 + 透传参数。

需求 1（删 Skill）：
- Modify `server/app/main.py` — 去掉 `skills_router` import + mount。
- Test `server/tests/test_skills_unmounted.py`（新建） — `GET /api/skills` → 404。
- Delete `web/src/features/ai-generation/SkillsPromptsTab.tsx`。
- Modify `web/src/features/ai-generation/AiGenerationWorkspace.tsx` — 去掉子 tab，直接渲染 `GenerateTab`。
- Modify `web/src/api/ai-generation.ts` — 删 `Skill` import + 5 个 skill 函数。
- Modify `web/src/types.ts` — 删 `Skill` 类型。

**休眠不动**（明确不删）：`server/app/modules/skills/*`、`server/app/modules/ai_generation/{models,service,schemas,pipeline}.py` 里的 skill 字段/路径、`skills` 表、`GenerationSession.skill_id`、`web/src/styles.css` 里的 skill class、`startGeneration/getGenerationSession/GenerationSession`（独立死代码，不在本次范围）。

---

## Task 1: `all_category_contexts(db)` —— 全部 bucket 候选列表

**Files:**
- Modify: `server/app/modules/articles/ai_format.py`（在 `_available_categories_for_article` 之后，约 224 行后新增）
- Test: `server/tests/test_ai_format.py`

- [ ] **Step 1: Write the failing test**

加到 `server/tests/test_ai_format.py` 末尾：

```python
@pytest.mark.mysql
def test_all_category_contexts_returns_all_buckets(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_format import all_category_contexts
        from server.app.modules.image_library.models import StockCategory

        with test_app.session_factory() as db:
            db.add(StockCategory(name="王者荣耀", bucket_name="wzry", description="MOBA"))
            db.add(StockCategory(name="原神", bucket_name="ys", description=None))
            db.commit()

        with test_app.session_factory() as db:
            cats = all_category_contexts(db)

        names = {c["name"] for c in cats}
        assert names == {"王者荣耀", "原神"}
        assert all(set(c.keys()) == {"id", "name", "description"} for c in cats)
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_ai_format.py::test_all_category_contexts_returns_all_buckets -q`
Expected: FAIL with `ImportError: cannot import name 'all_category_contexts'`

- [ ] **Step 3: Write minimal implementation**

在 `ai_format.py` 的 `_available_categories_for_article(...)` 函数之后插入：

```python
def all_category_contexts(db: Any) -> list[dict[str, Any]]:
    """返回系统里全部图片栏目（StockCategory）的 {id,name,description} 上下文。

    供方案自动配图：候选栏目取全部 bucket（而非文章已分配的类别），
    让模型按文章游戏内容自行匹配；匹配不上则返回空 image_positions。
    """
    from server.app.modules.image_library.models import StockCategory

    result: list[dict[str, Any]] = []
    for category in db.query(StockCategory).order_by(StockCategory.id.asc()).all():
        item = _category_context(category)
        if item is not None:
            result.append(item)
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_ai_format.py::test_all_category_contexts_returns_all_buckets -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/articles/ai_format.py server/tests/test_ai_format.py
git commit -m "feat(ai_format): all_category_contexts 返回全部图片 bucket 候选"
```

---

## Task 2: `run_ai_format(candidate_categories=...)` —— 用传入候选 bucket 配图

**Files:**
- Modify: `server/app/modules/articles/ai_format.py:419-472`（`_maybe_insert_images`）、`494-567`（`run_ai_format`）
- Test: `server/tests/test_ai_format.py`

- [ ] **Step 1: Write the failing test**

加到 `server/tests/test_ai_format.py` 末尾。验证：文章**没有**任何已分配类别，但通过 `candidate_categories` 传入一个 bucket 时，模型返回的 `image_positions` 能命中并插图。

```python
@pytest.mark.mysql
def test_run_ai_format_uses_candidate_categories_when_article_has_none(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        from server.app.modules.articles import ai_format as aif
        from server.app.modules.articles.ai_format import run_ai_format

        # 文章：两段正文，无任何 stock_category
        article = _create_article(
            client,
            {
                "type": "doc",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "王者荣耀是一款 MOBA 手游。"}]},
                    {"type": "paragraph", "content": [{"type": "text", "text": "它有上百名英雄。"}]},
                ],
            },
        )
        article_id = article["id"]

        # 模型返回：把节点 1 当配图位，category_id=777（候选里有）
        monkeypatch.setattr(
            "server.app.modules.articles.ai_format._call_litellm_completion",
            lambda **kw: _fake_completion(
                '{"heading_indices": [], "image_positions": [{"index": 1, "category_id": 777}]}'
            ),
        )
        monkeypatch.setattr(aif, "pick_image_id", lambda query, db: 1001)
        monkeypatch.setattr(
            aif,
            "fetch_image_by_id",
            lambda image_id, db: SimpleNamespace(
                url="http://img/1001.png", alt="王者荣耀", width=800, height=600
            ),
        )
        inserted = {}
        monkeypatch.setattr(
            aif,
            "insert_images_at_positions",
            lambda content_json, refs, positions: inserted.update(
                {"refs": refs, "positions": positions}
            )
            or content_json,
        )

        candidate = [{"id": 777, "name": "王者荣耀", "description": "MOBA"}]
        run_ai_format(
            article_id,
            include_images=True,
            candidate_categories=candidate,
        )

        assert inserted.get("positions") == [1]
        assert len(inserted.get("refs", [])) == 1
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_ai_format.py::test_run_ai_format_uses_candidate_categories_when_article_has_none -q`
Expected: FAIL with `TypeError: run_ai_format() got an unexpected keyword argument 'candidate_categories'`

- [ ] **Step 3a: 给 `_maybe_insert_images` 加可选 `available_categories`（保持向后兼容）**

把 `ai_format.py:419-427` 改为：

```python
def _maybe_insert_images(
    content_json: dict,
    parsed: dict,
    article: Any,
    db: Any,
    *,
    available_categories: list[dict[str, Any]] | None = None,
) -> tuple[dict, int]:
    if has_images_in_content(content_json):
        return content_json, 0

    cats = (
        available_categories
        if available_categories is not None
        else _available_categories_for_article(article, db)
    )
    category_ids: list[int] = [cat["id"] for cat in cats]
    if not category_ids:
        return content_json, 0
```

（其余函数体 428-472 不变。`available_categories=None` → 走 `_available_categories_for_article`，现有 10 个 `_maybe_insert_images` 单测不受影响。）

- [ ] **Step 3b: 给 `run_ai_format` 加 `candidate_categories` 参数并透传**

把 `ai_format.py:494-501` 的签名改为：

```python
def run_ai_format(
    article_id: int,
    *,
    include_images: bool = False,
    lock_started_at: datetime | None = None,
    preset_id: int | None = None,
    user_id: int | None = None,
    candidate_categories: list[dict[str, Any]] | None = None,
) -> None:
    """Identify body subheadings and write the updated Tiptap document back to the article.

    candidate_categories 非 None 时用它当配图候选栏目（方案自动配图用全部 bucket）；
    None 时回退到文章已分配的类别（手动 AI 排版按钮的现状行为）。
    """
```

把 `ai_format.py:534-536` 的 `available_categories` 计算改为：

```python
        if not include_images:
            available_categories = []
        elif candidate_categories is not None:
            available_categories = candidate_categories
        else:
            available_categories = _available_categories_for_article(article, db)
```

把 `ai_format.py:564-567` 的 `_maybe_insert_images` 调用改为透传同一份候选：

```python
        if include_images:
            new_content_json, image_count = _maybe_insert_images(
                new_content_json, parsed, article, db, available_categories=available_categories
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_ai_format.py::test_run_ai_format_uses_candidate_categories_when_article_has_none -q`
Expected: PASS

- [ ] **Step 5: 回归既有 ai_format 测试（确认 0 破坏）**

Run: `pytest server/tests/test_ai_format.py -q`
Expected: 全绿（既有 `_maybe_insert_images` / `run_ai_format` 用例不变）

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/articles/ai_format.py server/tests/test_ai_format.py
git commit -m "feat(ai_format): run_ai_format 支持 candidate_categories 用全部 bucket 配图"
```

---

## Task 3: 方案生文成功后自动排版/配图（hook 进 scheme_executor）

**Files:**
- Modify: `server/app/modules/ai_generation/scheme_executor.py`（顶部 import + 新增 `_auto_format_article` + `_execute_task` 收尾调用）
- Test: `server/tests/test_scheme_autoformat.py`（新建）

- [ ] **Step 1: Write the failing test**

新建 `server/tests/test_scheme_autoformat.py`：

```python
"""方案生文后自动 AI 排版/配图：_auto_format_article 设锁 + 透传全部 bucket。"""

from __future__ import annotations

import pytest

from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_auto_format_article_sets_lock_and_passes_all_buckets(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        from server.app.modules.ai_generation import scheme_executor
        from server.app.modules.articles.models import Article
        from server.app.modules.image_library.models import StockCategory
        from server.app.modules.system.models import User

        # 文章（有正文）
        resp = client.post(
            "/api/articles",
            json={
                "title": "auto format",
                "content_json": {
                    "type": "doc",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "原神是一款开放世界游戏。"}]}
                    ],
                },
            },
        )
        assert resp.status_code == 200
        article_id = resp.json()["id"]

        # 一个 bucket + 给当前用户设 ai_format_preset_id=42
        with test_app.session_factory() as db:
            db.add(StockCategory(name="原神", bucket_name="ys", description=None))
            user = db.query(User).first()
            user_id = user.id
            user.ai_format_preset_id = 42
            db.commit()

        captured = {}

        def fake_run_ai_format(aid, **kwargs):
            captured["article_id"] = aid
            captured.update(kwargs)

        monkeypatch.setattr(scheme_executor, "run_ai_format", fake_run_ai_format)

        scheme_executor._auto_format_article(article_id, user_id, test_app.session_factory)

        # 透传断言
        assert captured["article_id"] == article_id
        assert captured["include_images"] is True
        assert captured["preset_id"] == 42
        assert captured["user_id"] == user_id
        assert {c["name"] for c in captured["candidate_categories"]} == {"原神"}
        assert captured["lock_started_at"] is not None

        # 锁被置上
        with test_app.session_factory() as db:
            art = db.get(Article, article_id)
            assert art.ai_checking is True
            assert art.ai_checking_started_at == captured["lock_started_at"]
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_scheme_autoformat.py -q`
Expected: FAIL with `AttributeError: module ... has no attribute '_auto_format_article'`

- [ ] **Step 3a: scheme_executor 顶部加 import**

在 `scheme_executor.py` 的 import 区（约 28 行 `get_visible_prompt_template` 之后）加：

```python
from server.app.articles_autoformat_imports import noop  # placeholder — remove if unused
```

实际加这两行（替换上面占位说明，直接写）：

```python
from server.app.modules.articles.ai_format import all_category_contexts, run_ai_format
from server.app.core.time import utcnow  # 已在文件顶部 import，可省略重复
```

> 注：`scheme_executor.py` 顶部已 `from server.app.core.time import utcnow`，只需新增 `from server.app.modules.articles.ai_format import all_category_contexts, run_ai_format` 一行。`articles.ai_format` 不反向 import `ai_generation`，无循环依赖。

- [ ] **Step 3b: 新增 `_auto_format_article`**

在 `scheme_executor.py` 的 `_aggregate_run` 之后（文件末尾）新增：

```python
def _auto_format_article(
    article_id: int,
    user_id: int,
    session_factory: SessionFactory,
) -> None:
    """方案生文成功后自动 AI 排版 + 用全部图片 bucket 智能配图。

    best-effort：任何失败只记日志，绝不影响已生成的文章 / task 状态。
    """
    try:
        from server.app.modules.articles.ai_format import has_ai_format_targets
        from server.app.modules.articles.models import Article
        from server.app.modules.system.models import User

        lock_started_at = utcnow().replace(microsecond=0)
        preset_id: int | None = None
        candidate_categories: list[Any] = []

        db = session_factory()
        try:
            article = db.get(Article, article_id)
            if article is None or article.is_deleted:
                return
            if not has_ai_format_targets(article.content_json):
                return
            user = db.get(User, user_id)
            preset_id = getattr(user, "ai_format_preset_id", None) if user else None
            candidate_categories = all_category_contexts(db)
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
            preset_id=preset_id,
            user_id=user_id,
            candidate_categories=candidate_categories,
        )
    except Exception:  # noqa: BLE001 — 自动排版失败不影响生文结果
        logger.exception("auto ai_format failed for article %s", article_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_scheme_autoformat.py -q`
Expected: PASS

- [ ] **Step 5: hook 进 `_execute_task`（标 task done 之后）**

把 `scheme_executor.py:161-170` 的收尾块改为（在 `db.commit()` 关闭 session 之后、`return article_id` 之前调用自动排版）：

```python
    db = session_factory()
    try:
        task = db.get(GenerationSchemeRunTask, task_id)
        task.status = "done"
        task.article_id = article_id
        task.completed_at = utcnow()
        db.commit()
    finally:
        db.close()

    # 生文成功后自动 AI 排版 + 全 bucket 智能配图（best-effort，不影响 task 结果）
    _auto_format_article(article_id, user_id, session_factory)
    return article_id
```

- [ ] **Step 6: 回归 scheme executor 既有测试**

Run: `pytest server/tests/ -q -k scheme`
Expected: 全绿（自动排版被 hook 后既有 run/task 状态机用例仍通过；若既有测试未 mock `run_ai_format` 但也没配 format API key，`_auto_format_article` 内部异常被吞，不影响 task=done）

- [ ] **Step 7: Commit**

```bash
git add server/app/modules/ai_generation/scheme_executor.py server/tests/test_scheme_autoformat.py
git commit -m "feat(scheme): 方案生文成功后自动 AI 排版 + 全 bucket 智能配图"
```

---

## Task 4: `/api/skills` 下线（unmount，保留模块文件休眠）

**Files:**
- Modify: `server/app/main.py:58`（import）、`server/app/main.py:213-217`（mount）
- Test: `server/tests/test_skills_unmounted.py`（新建）

- [ ] **Step 1: Write the failing test**

新建 `server/tests/test_skills_unmounted.py`：

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_skills_unmounted.py -q`
Expected: FAIL（当前 `/api/skills` 返回 200，断言 404 失败）

- [ ] **Step 3: 从 main.py 去掉 import + mount**

删除 `server/app/main.py:58` 这一行：

```python
from server.app.modules.skills.router import router as skills_router
```

删除 `server/app/main.py:213-217` 的 mount 块（`app.include_router(skills_router, prefix="/api/skills", tags=["skills"])` 整段）。

> `server/app/modules/skills/*` 文件、`skills` 表、`GenerationSession.skill_id` 保持不动（休眠）。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_skills_unmounted.py -q`
Expected: PASS

- [ ] **Step 5: 确认后端无引用残留 + 冒烟启动**

Run: `python -c "import server.app.main as m; m.create_app()"`
Expected: 无异常（`create_app()` 正常构建，说明删除 import/mount 没留悬空引用）

- [ ] **Step 6: Commit**

```bash
git add server/app/main.py server/tests/test_skills_unmounted.py
git commit -m "chore(skills): 下线 /api/skills 路由（模块文件与表保留休眠）"
```

---

## Task 5: 前端删除「技能与提示词」子 tab + skill API/类型

**Files:**
- Delete: `web/src/features/ai-generation/SkillsPromptsTab.tsx`
- Modify: `web/src/features/ai-generation/AiGenerationWorkspace.tsx`（全量替换）
- Modify: `web/src/api/ai-generation.ts:14`（删 `Skill` import）、`25-59`（删 5 个 skill 函数）
- Modify: `web/src/types.ts:8-16`（删 `Skill` 类型）

- [ ] **Step 1: 删除 SkillsPromptsTab 文件**

```bash
git rm web/src/features/ai-generation/SkillsPromptsTab.tsx
```

- [ ] **Step 2: 简化 AiGenerationWorkspace（去掉子 tab，直接渲染 GenerateTab）**

把 `web/src/features/ai-generation/AiGenerationWorkspace.tsx` 全量替换为：

```tsx
import { GenerateTab } from "./GenerateTab";

export function AiGenerationWorkspace({ onNavigateToContent }: { onNavigateToContent: () => void }) {
  return (
    <div className="aiWorkspace">
      <div className="topbar" style={{ marginBottom: 0 }}>
        <div>
          <p className="eyebrow">AI 生文</p>
          <h1>智能创作</h1>
        </div>
      </div>

      <div className="aiTabContent">
        <GenerateTab onNavigateToContent={onNavigateToContent} />
      </div>
    </div>
  );
}
```

- [ ] **Step 3: 从 api/ai-generation.ts 删 Skill import + skill 函数**

删除 `web/src/api/ai-generation.ts:14` 的 `  Skill,`（types import 列表里那一行）。

删除 `web/src/api/ai-generation.ts:25-59` 的五个函数：`listSkills` / `createSkill` / `updateSkill` / `patchSkill` / `deleteSkill`（整段，到 `deleteSkill` 的 `}` 为止）。保留其后的 `startGeneration` 等不动。

- [ ] **Step 4: 从 types.ts 删 Skill 类型**

删除 `web/src/types.ts:8-16` 的 `export type Skill = { ... };` 整段（保留其上的 `PromptScope` 与其下的 `PromptTemplate`）。

- [ ] **Step 5: typecheck（硬门禁）**

Run: `pnpm --filter @geo/web typecheck`
Expected: PASS（无 `Skill` / `SkillsPromptsTab` 悬空引用；若报错说明还有未清理的 import，按报错文件清掉）

- [ ] **Step 6: build（硬门禁）**

Run: `pnpm --filter @geo/web build`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add web/src/features/ai-generation/AiGenerationWorkspace.tsx web/src/api/ai-generation.ts web/src/types.ts
git commit -m "feat(ai-generation): 删除「技能与提示词」子 tab + skill 前端 API/类型"
```

---

## Task 6: 全量回归 + CI 硬门禁

- [ ] **Step 1: 后端测试（MySQL）**

Run: `pytest server/tests/ -q`（需 `GEO_TEST_DATABASE_URL`，DB 名含 `test`）
Expected: 全绿

- [ ] **Step 2: 后端 lint / format / 类型**

Run:
```bash
ruff check server/
ruff format --check server/
mypy server/app
```
Expected: 三项均通过

- [ ] **Step 3: 前端 typecheck + build**

Run:
```bash
pnpm --filter @geo/web typecheck
pnpm --filter @geo/web build
```
Expected: 均通过

- [ ] **Step 4: 端到端冒烟（可选，本地）**

起 `uvicorn server.app.main:app --port 8000`，建池→建 scheme→`POST /api/generation/schemes/{id}/runs`→`GET /api/generation/scheme-runs/{run_id}`，确认 run=`done` 且生成文章的 `content_json` 已被排版（段落升级为 H2），若系统里有匹配 bucket 且配了 `GEO_AI_FORMAT_*` 则正文出现配图。

---

## Self-Review

**Spec coverage:**
- 需求 1「删 AI生文 tab 里的技能与提示词、移到提示词管理」→ Task 5（删子 tab）；「提示词管理」已天然覆盖 generation + ai_format 两 scope（`PromptsWorkspace`），无需新增「移动」动作。✓
- 需求 1「删 skill 管理、生文只留提示词、AI格式提示词保留」→ Task 4（unmount `/api/skills`）+ Task 5（删前端）；ai_format scope 提示词在 `PromptsWorkspace` 保留不动。✓
- 需求 1「skill 留休眠」→ 决策 1：不 drop 表、不删模块文件、不写迁移。✓
- 需求 2a「生文后自动调 AI 格式」→ Task 3（`_execute_task` 收尾 hook）。✓
- 需求 2b「识别游戏内容与 bucket 匹配、做 bucket 列表传入提示词、再决定插图」→ Task 1（全部 bucket 列表）+ Task 2（传入 `candidate_categories`，prompt 模板已含「按内容匹配栏目、不确定就不插」语义）。✓
- 决策 2「用该用户的 preset」→ Task 3 `_auto_format_article` 取 `User.ai_format_preset_id`。✓
- 决策 3「只插图、不写回类别」→ Task 3 不写 `article.stock_categories`，仅 `run_ai_format` 内部插图。✓

**Placeholder scan:** Task 3 Step 3a 出现一处占位说明行，已在同 step 内用「实际加这一行」明确指出最终只加 `from server.app.modules.articles.ai_format import all_category_contexts, run_ai_format`——执行时以该说明为准，勿写入占位 import。其余步骤均为可直接落地的完整代码。

**Type/signature consistency:**
- `run_ai_format(..., candidate_categories=...)`（Task 2）与 `_auto_format_article` 调用处（Task 3）参数名一致。✓
- `_maybe_insert_images(..., available_categories=...)`（Task 2 Step 3a）与 `run_ai_format` 调用处（Task 2 Step 3b）一致。✓
- `all_category_contexts(db)`（Task 1）返回 `{id,name,description}`，与 prompt 模板 `available_categories` 字段（`category.id/name/description`）一致。✓
- `scheme_executor.run_ai_format`（顶部 import）被测试 monkeypatch 的路径一致（Task 3 测试 patch `scheme_executor.run_ai_format`）。✓
