# `/goal` Loop 配图对齐 Web UI + 进度日志中文化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修两个 bug — ① /goal Loop 生成的文章无配图 + 无封面（根因：调简陋 `illustrate_article_mcp` 而非 Web UI 同款的 `run_ai_format`）；② 主对话进度日志 6 行英文混搭。一次 PR 解决，bundle version v1→v2。

**Architecture:** 抽取 pipeline `ai_illustrate` 节点的 `_format_one` + `_maybe_set_cover` 到共享 service `articles/ai_illustrate_svc.py`；pipeline 节点改用 service，新增 MCP endpoint `POST /api/articles/{id}/ai-illustrate` 也调它——两条路径配图效果完全一致。writer skill 改调新 MCP 工具 `ai_illustrate_article(article_id, main_category_id)`，main_category_id 写死在矩阵特例段。orchestrator skill 6 行进度日志中文化。

**Tech Stack:** FastAPI + Pydantic v2 / FastMCP / SQLAlchemy 2.x / pytest `@pytest.mark.mysql` / 现有 `articles.ai_format` 模块 / 现有 `image_library.cover` 模块

**Spec:** [`docs/superpowers/specs/2026-06-25-loop-illustration-and-i18n-fix-design.md`](../specs/2026-06-25-loop-illustration-and-i18n-fix-design.md)

**Branch:** `fix/loop-illustration-and-i18n`（已从 `origin/main` (f46a5a1) 拉出，spec 已 commit 在 `e9b63b6`）

---

## Files to Touch

| 文件 | 操作 | 责任 |
|---|---|---|
| `server/app/modules/articles/ai_illustrate_svc.py` | 新建 | 单篇文章「AI 配图 + 自动封面」共享 service；`illustrate_one(...) -> IllustrateResult` |
| `server/app/modules/pipelines/nodes/ai_illustrate.py` | 修改 | 删 `_format_one` + `_maybe_set_cover`，`_one` 改 1 行调 service；NodeResult.output schema 不变 |
| `server/app/modules/articles/router.py` | 修改 | 追加 `POST /{id}/ai-illustrate` MCP endpoint |
| `server/mcp/tools/action.py` | 修改 | 追加 `ai_illustrate_article` MCP 工具 |
| `server/app/modules/loop_skills/templates/skills/geo-article-writer/SKILL.md` | 修改 | step 5 改调新工具 + 矩阵特例段加 `default_main_category_id` |
| `server/app/modules/loop_skills/templates/skills/geo-goal-orchestrator/SKILL.md` | 修改 | 进度日志 6 行格式全中文化 |
| `server/app/modules/loop_skills/templates/README.md` | 修改 | onboarding 加 step 6（首次装 skill 后填 main_category_id） |
| `server/app/modules/loop_skills/version.py` | 修改 | bump `LOOP_SKILL_BUNDLE_VERSION` v1→v2，KNOWN_BUNDLE_SHAS 加新 sha（v1 保留） |
| `server/tests/test_ai_illustrate_svc.py` | 新建 | service 6 个单测（mock run_ai_format + set_cover） |
| `server/tests/test_articles_ai_illustrate_endpoint.py` | 新建 | endpoint 2 个集成测试（401 鉴权 + 200 mock service） |
| `server/tests/test_pipeline_ai_illustrate.py` | 新建 | pipeline 节点 snapshot 测试（NodeResult.output 6 字段稳定） |

**关键边界**：
- service 是纯调度层：调 `run_ai_format` + `set_random_cover_from_category`，自己不重复实现配图逻辑
- pipeline 节点对外 schema 完全不变（前端 / `agent_run_logs` 零改动）
- MCP endpoint 与现有 `illustrate_article_mcp` **并列保留**，不删旧的（向后兼容 generation-loop.md）
- skill 文件改动会 bump bundle sha → 必须 bump version + 加 sha 到 KNOWN

---

## Task 1: 共享 service `articles/ai_illustrate_svc.py`（TDD）

**Files:**
- Create: `server/app/modules/articles/ai_illustrate_svc.py`
- Test: `server/tests/test_ai_illustrate_svc.py`（新建）

- [ ] **Step 1: 写失败的测试（6 用例）**

创建 `server/tests/test_ai_illustrate_svc.py`：

```python
"""ai_illustrate_svc 单篇文章配图 + 自动封面 service 测试。

mock 掉 run_ai_format 和 set_random_cover_from_category —— 此处只测调度逻辑，
配图本身的正确性由 articles.ai_format 自己的测试覆盖。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from server.tests.utils import build_test_app


def _mk_article(test_app, *, title: str = "t", content_json: dict | None = None) -> int:
    """建一篇带 ai_format_targets 的最小 article。"""
    from server.app.modules.articles.models import Article

    db = test_app.session_factory()
    try:
        a = Article(
            user_id=test_app.admin_id,
            title=title,
            # 含 heading 节点 → has_ai_format_targets 返 True
            content_json=content_json
            or {
                "type": "doc",
                "content": [
                    {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "标题"}]},
                    {"type": "paragraph", "content": [{"type": "text", "text": "段落"}]},
                ],
            },
            content_html="",
            plain_text="标题 段落",
            word_count=4,
            status="draft",
            review_status="pending",
        )
        db.add(a)
        db.commit()
        return a.id
    finally:
        db.close()


def _patch_run_ai_format(monkeypatch, return_value: int = 3) -> list:
    """Mock run_ai_format → 返指定 images_inserted；记录调用参数。"""
    calls: list[dict[str, Any]] = []

    def fake(article_id, **kwargs):
        calls.append({"article_id": article_id, **kwargs})
        return return_value

    monkeypatch.setattr(
        "server.app.modules.articles.ai_illustrate_svc.run_ai_format", fake
    )
    return calls


def _patch_cover(monkeypatch, status: str = "set", error: str | None = None) -> list:
    """Mock set_random_cover_from_category → 返指定 CoverResult；记录参数。"""
    from server.app.modules.image_library.cover import CoverResult

    calls: list[dict[str, Any]] = []

    def fake(db, article, category_id, user_id):
        calls.append({"category_id": category_id, "user_id": user_id})
        return CoverResult(status=status, error=error)

    monkeypatch.setattr(
        "server.app.modules.articles.ai_illustrate_svc.set_random_cover_from_category",
        fake,
    )
    return calls


@pytest.mark.mysql
def test_illustrate_one_happy_path_returns_images_and_set_cover(monkeypatch):
    """run_ai_format 返 3 + cover set → result.images_inserted=3, cover_status=set。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_illustrate_svc import (
            IllustrateOptions,
            illustrate_one,
        )

        aid = _mk_article(test_app)
        fmt_calls = _patch_run_ai_format(monkeypatch, return_value=3)
        cover_calls = _patch_cover(monkeypatch, status="set")

        result = illustrate_one(
            article_id=aid,
            main_category_id=42,
            user_id=test_app.admin_id,
            options=IllustrateOptions(),
            session_factory=test_app.session_factory,
        )

        assert result.article_id == aid
        assert result.images_inserted == 3
        assert result.cover_status == "set"
        assert result.cover_error is None
        assert result.format_error is None
        assert len(fmt_calls) == 1
        assert fmt_calls[0]["candidate_categories"] is not None  # category_contexts_for 输出
        assert len(cover_calls) == 1
        assert cover_calls[0]["category_id"] == 42
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_illustrate_one_article_missing_returns_format_error(monkeypatch):
    """article_id 不存在 → format_error="article not found or deleted"。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_illustrate_svc import (
            IllustrateOptions,
            illustrate_one,
        )

        _patch_run_ai_format(monkeypatch)
        _patch_cover(monkeypatch)

        result = illustrate_one(
            article_id=999999,
            main_category_id=1,
            user_id=test_app.admin_id,
            options=IllustrateOptions(),
            session_factory=test_app.session_factory,
        )

        assert result.images_inserted == 0
        assert result.format_error == "article not found or deleted"
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_illustrate_one_no_ai_format_targets_returns_format_error(monkeypatch):
    """文章只有 paragraph 无 heading → has_ai_format_targets=False → format_error。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_illustrate_svc import (
            IllustrateOptions,
            illustrate_one,
        )

        aid = _mk_article(
            test_app,
            content_json={"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "x"}]}]},
        )
        _patch_run_ai_format(monkeypatch)
        _patch_cover(monkeypatch)

        result = illustrate_one(
            article_id=aid,
            main_category_id=1,
            user_id=test_app.admin_id,
            options=IllustrateOptions(),
            session_factory=test_app.session_factory,
        )

        assert result.format_error == "no ai_format_targets in content"
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_illustrate_one_cover_error_surfaces_in_result(monkeypatch):
    """cover 失败 → cover_status=error + cover_error 非空，不影响 images_inserted。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_illustrate_svc import (
            IllustrateOptions,
            illustrate_one,
        )

        aid = _mk_article(test_app)
        _patch_run_ai_format(monkeypatch, return_value=2)
        _patch_cover(monkeypatch, status="error", error="minio timeout")

        result = illustrate_one(
            article_id=aid,
            main_category_id=1,
            user_id=test_app.admin_id,
            options=IllustrateOptions(),
            session_factory=test_app.session_factory,
        )

        assert result.images_inserted == 2  # 配图阶段照常成功
        assert result.cover_status == "error"
        assert result.cover_error == "minio timeout"
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_illustrate_one_set_cover_false_skips_cover_stage(monkeypatch):
    """options.set_cover=False → 不调 set_random_cover，cover_status=skipped。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_illustrate_svc import (
            IllustrateOptions,
            illustrate_one,
        )

        aid = _mk_article(test_app)
        _patch_run_ai_format(monkeypatch, return_value=1)
        cover_calls = _patch_cover(monkeypatch)

        result = illustrate_one(
            article_id=aid,
            main_category_id=1,
            user_id=test_app.admin_id,
            options=IllustrateOptions(set_cover=False),
            session_factory=test_app.session_factory,
        )

        assert result.cover_status == "skipped"
        assert result.cover_error is None
        assert len(cover_calls) == 0  # cover 函数没被调
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_illustrate_one_reads_back_article_ai_format_error(monkeypatch):
    """run_ai_format 把错误吞掉写到 article.ai_format_error → service 阶段 3 回读出来。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_illustrate_svc import (
            IllustrateOptions,
            illustrate_one,
        )
        from server.app.modules.articles.models import Article

        aid = _mk_article(test_app)

        # fake run_ai_format 返 0 同时往 article.ai_format_error 写一条
        def fake(article_id, **kwargs):
            db = test_app.session_factory()
            try:
                article = db.get(Article, article_id)
                article.ai_format_error = "LLM timeout after 60s"
                db.commit()
            finally:
                db.close()
            return 0

        monkeypatch.setattr(
            "server.app.modules.articles.ai_illustrate_svc.run_ai_format", fake
        )
        _patch_cover(monkeypatch)

        result = illustrate_one(
            article_id=aid,
            main_category_id=1,
            user_id=test_app.admin_id,
            options=IllustrateOptions(),
            session_factory=test_app.session_factory,
        )

        assert result.images_inserted == 0
        assert result.format_error == "LLM timeout after 60s"
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 跑测试，确认 6 个全 fail**

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_ai_illustrate_svc.py -q
```

**预期**：6 个测试 fail，提示 `ModuleNotFoundError: No module named 'server.app.modules.articles.ai_illustrate_svc'`。

- [ ] **Step 3: 创建 `ai_illustrate_svc.py`**

`server/app/modules/articles/ai_illustrate_svc.py`:

```python
"""ai_illustrate_svc —— 单篇文章「AI 智能配图 + 自动封面」的共享 service.

被 pipelines/nodes/ai_illustrate.py 和 articles MCP endpoint 共用，
保证两条路径配图效果完全一致.

不并发（单文章），调用方按需自管 ThreadPoolExecutor 包多篇.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from server.app.core.time import utcnow
from server.app.modules.articles.ai_format import (
    category_contexts_for,
    has_ai_format_targets,
    run_ai_format,
)
from server.app.modules.articles.models import Article
from server.app.modules.image_library.cover import (
    CoverResult,
    set_random_cover_from_category,
)

_logger = logging.getLogger(__name__)


@dataclass
class IllustrateOptions:
    """配图旋钮，跟 pipeline ai_illustrate 节点的 cfg 字段一一对应."""

    include_companion: bool = True
    web_fallback: bool = False
    aggressive_images: bool = True
    max_images: int | None = None
    min_spacing: int | None = None
    preset_id: int | None = None
    set_cover: bool = True


@dataclass
class IllustrateResult:
    article_id: int
    images_inserted: int = 0
    cover_status: str = "skipped"
    cover_error: str | None = None
    format_error: str | None = None


def illustrate_one(
    *,
    article_id: int,
    main_category_id: int,
    user_id: int,
    options: IllustrateOptions,
    session_factory: Callable[[], Session],
) -> IllustrateResult:
    """给一篇文章配图 + 设封面，复用 pipeline ai_illustrate 节点的成熟逻辑.

    session_factory 而非 db：开两个独立短 session（配图持锁 / 封面独立提交），
    跟节点里的 _format_one + _maybe_set_cover 等价.
    """
    aggressive = options.aggressive_images
    builtin_variant = "aggressive" if aggressive else "conservative"
    max_images = (
        options.max_images
        if (options.max_images and options.max_images > 0)
        else (12 if aggressive else 3)
    )
    min_spacing = (
        options.min_spacing
        if (options.min_spacing and options.min_spacing > 0)
        else (1 if aggressive else 5)
    )

    # 阶段 1: 配图 (持锁 + run_ai_format)
    lock_started_at = utcnow().replace(microsecond=0)
    candidate_categories: list = []

    db = session_factory()
    try:
        article = db.get(Article, article_id)
        if article is None or article.is_deleted:
            return IllustrateResult(
                article_id=article_id, format_error="article not found or deleted"
            )
        if not has_ai_format_targets(article.content_json):
            return IllustrateResult(
                article_id=article_id, format_error="no ai_format_targets in content"
            )
        candidate_categories = category_contexts_for(
            db,
            main_category_id=main_category_id,
            include_companion=options.include_companion,
        )
        article.ai_checking = True
        article.ai_checking_started_at = lock_started_at
        article.ai_format_error = None
        db.commit()
    finally:
        db.close()

    images_inserted = run_ai_format(
        article_id,
        include_images=True,
        lock_started_at=lock_started_at,
        preset_id=options.preset_id,
        user_id=user_id,
        candidate_categories=candidate_categories,
        web_fallback=options.web_fallback,
        max_images=max_images,
        min_spacing=min_spacing,
        builtin_variant=builtin_variant,
    )

    # 阶段 2: 封面 (独立短 session)
    cover_status = "skipped"
    cover_error: str | None = None
    if options.set_cover:
        db = session_factory()
        try:
            article = db.get(Article, article_id)
            if article is not None and not article.is_deleted:
                result: CoverResult = set_random_cover_from_category(
                    db, article, main_category_id, user_id
                )
                db.commit()
                cover_status = result.status
                cover_error = result.error
        except Exception as exc:  # noqa: BLE001 — best-effort
            db.rollback()
            cover_status = "error"
            cover_error = str(exc)
        finally:
            db.close()

    # 阶段 3: 回读 article.ai_format_error
    format_error: str | None = None
    db = session_factory()
    try:
        article = db.get(Article, article_id)
        if article is not None:
            format_error = article.ai_format_error
    finally:
        db.close()

    return IllustrateResult(
        article_id=article_id,
        images_inserted=images_inserted or 0,
        cover_status=cover_status,
        cover_error=cover_error,
        format_error=format_error,
    )
```

- [ ] **Step 4: 跑测试，确认 6 个全 pass**

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_ai_illustrate_svc.py -q
```

**预期**：`6 passed`。

- [ ] **Step 5: ruff + format clean**

```bash
docker compose exec app ruff check server/app/modules/articles/ai_illustrate_svc.py server/tests/test_ai_illustrate_svc.py
docker compose exec app ruff format --check server/app/modules/articles/ai_illustrate_svc.py server/tests/test_ai_illustrate_svc.py
```

如 format 报差异，去掉 `--check` 直接改写。

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/articles/ai_illustrate_svc.py server/tests/test_ai_illustrate_svc.py
git commit -m "$(cat <<'EOF'
feat(articles): ai_illustrate_svc 单篇配图共享 service + 6 个 TDD 测试

抽取 pipeline ai_illustrate 节点的 _format_one + _maybe_set_cover 到独立
service，单文章 illustrate_one(article_id, main_category_id, options,
user_id, session_factory) → IllustrateResult。三阶段：
- 阶段 1：持 ai_checking 锁 + run_ai_format（AI 决定哪些图哪里）
- 阶段 2：set_random_cover_from_category（独立短 session 提交封面）
- 阶段 3：回读 article.ai_format_error 暴露 run_ai_format 吞掉的错误

为下一步 Task 2 pipeline 节点切换 + Task 3 新 MCP endpoint 复用做准备。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: pipeline `ai_illustrate.py` 节点改用 service

**Files:**
- Modify: `server/app/modules/pipelines/nodes/ai_illustrate.py`
- Test: `server/tests/test_pipeline_ai_illustrate.py`（新建——snapshot 校验 NodeResult.output schema）

- [ ] **Step 1: 写 pipeline snapshot 测试**

创建 `server/tests/test_pipeline_ai_illustrate.py`：

```python
"""pipeline ai_illustrate 节点 snapshot 测试。

Task 2 把节点内部 _format_one + _maybe_set_cover 抽到 ai_illustrate_svc.illustrate_one；
此测试通过 mock service 返指定 IllustrateResult，断言节点 NodeResult.output 6 字段
schema 完全不变（前端 / agent_run_logs 展示逻辑零改动）。
"""

from __future__ import annotations

import pytest

from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_ai_illustrate_node_output_schema_stable(monkeypatch):
    """节点 output 含 6 个固定 key：article_ids / errors / images_inserted /
    format_errors / covers_set / cover_errors。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.articles.ai_illustrate_svc import IllustrateResult
        from server.app.modules.pipelines.nodes.ai_illustrate import run_ai_illustrate
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        # 准备 2 篇 article id 走 mock service
        from server.app.modules.articles.models import Article

        db = test_app.session_factory()
        try:
            a1 = Article(
                user_id=test_app.admin_id,
                title="t1",
                content_json={
                    "type": "doc",
                    "content": [{"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "h"}]}],
                },
                content_html="",
                plain_text="",
                word_count=1,
                status="draft",
                review_status="pending",
            )
            a2 = Article(
                user_id=test_app.admin_id,
                title="t2",
                content_json={
                    "type": "doc",
                    "content": [{"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "h"}]}],
                },
                content_html="",
                plain_text="",
                word_count=1,
                status="draft",
                review_status="pending",
            )
            db.add(a1)
            db.add(a2)
            db.commit()
            ids = [a1.id, a2.id]
        finally:
            db.close()

        # mock service：第一篇 set 封面 + 2 张图，第二篇 cover error
        def fake_illustrate_one(*, article_id, **kwargs):
            if article_id == ids[0]:
                return IllustrateResult(
                    article_id=article_id, images_inserted=2, cover_status="set"
                )
            return IllustrateResult(
                article_id=article_id,
                images_inserted=1,
                cover_status="error",
                cover_error="minio down",
            )

        monkeypatch.setattr(
            "server.app.modules.pipelines.nodes.ai_illustrate.illustrate_one",
            fake_illustrate_one,
        )

        ctx = NodeRunContext(
            session_factory=test_app.session_factory,
            user_id=test_app.admin_id,
            config={"main_category_id": 1},  # 必填
            inputs={"article_ids": ids},
            upstream={},
        )
        result = run_ai_illustrate(ctx)

        output = result.output
        # 6 个固定字段都在
        assert set(output.keys()) >= {
            "article_ids",
            "errors",
            "images_inserted",
            "format_errors",
            "covers_set",
            "cover_errors",
        }
        assert output["article_ids"] == ids
        assert output["images_inserted"] == 3  # 2 + 1
        assert output["covers_set"] == 1  # 只 a1
        assert any("minio down" in e for e in output["cover_errors"])
        assert output["errors"] == []  # 没未捕获异常
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 跑测试，确认 fail**

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_pipeline_ai_illustrate.py -q
```

**预期**：fail。原因：当前 ai_illustrate.py 没有 import `illustrate_one`，monkeypatch 找不到目标 → AttributeError。

- [ ] **Step 3: 改 pipeline 节点用 service**

修改 `server/app/modules/pipelines/nodes/ai_illustrate.py`：

(a) 顶部 import 区改为：

```python
"""ai_illustrate 处理节点（前端「AI配图」）：给上游文章自动配图。

复用 articles/ai_illustrate_svc.py 的 illustrate_one——pipeline 节点和 /goal
MCP loop 都调它，保证两条路径配图效果一致。

并发 max_workers=4，单篇失败收进 errors（partial_failed），不中断。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from server.app.modules.articles.ai_illustrate_svc import (
    IllustrateOptions,
    IllustrateResult,
    illustrate_one,
)
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError
```

> **删除** 原来的 `from server.app.core.time import utcnow` / `from server.app.modules.articles.ai_format import ...` 这些 import（service 接管了）。

(b) 把 `run_ai_illustrate` 函数体里 `_format_one` 和 `_maybe_set_cover` **两个内部函数定义全部删除**（约 -55 行），用下面的 `_one` 替换：

```python
    def _one(article_id: int) -> IllustrateResult:
        return illustrate_one(
            article_id=article_id,
            main_category_id=main_category_id,
            user_id=ctx.user_id,
            options=IllustrateOptions(
                include_companion=include_companion,
                web_fallback=web_fallback,
                aggressive_images=aggressive,
                max_images=max_images,
                min_spacing=min_spacing,
                preset_id=effective_preset,
                set_cover=set_cover,
            ),
            session_factory=ctx.session_factory,
        )
```

(c) 把聚合循环改为消费 `IllustrateResult`（替换原来 try/result/images/cover 解包的写法）：

```python
    images_inserted = 0
    covers_set = 0
    cover_errors: list[str] = []
    format_errors_from_results: list[str] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_one, aid): aid for aid in article_ids}
        for fut in as_completed(futures):
            try:
                result = fut.result()
                images_inserted += result.images_inserted
                if result.cover_status == "set":
                    covers_set += 1
                elif result.cover_status == "error" and result.cover_error:
                    cover_errors.append(f"article {result.article_id}: {result.cover_error}")
                if result.format_error:
                    format_errors_from_results.append(
                        f"article {result.article_id}: {result.format_error}"
                    )
            except Exception as exc:  # 单篇未捕获异常不中断
                errors.append(f"article {futures[fut]}: {exc}")
```

(d) 把原来 `format_errors = _collect_format_errors(ctx, article_ids)` 这一行**删除**——service 阶段 3 已经回读了。把 `_collect_format_errors` helper 函数也**删除**（service 取代了）。

(e) 改 NodeResult 输出：

```python
    return NodeResult(
        output={
            "article_ids": article_ids,
            "errors": errors,
            "images_inserted": images_inserted,
            "format_errors": format_errors_from_results,
            "covers_set": covers_set,
            "cover_errors": cover_errors,
        },
        article_ids=article_ids,
    )
```

- [ ] **Step 4: 跑测试，确认 pass**

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_pipeline_ai_illustrate.py server/tests/test_ai_illustrate_svc.py -q
```

**预期**：`7 passed`（6 service + 1 pipeline snapshot）。

- [ ] **Step 5: 确认 ai_illustrate.py 没残留无用 import / 死代码**

```bash
docker compose exec app ruff check server/app/modules/pipelines/nodes/ai_illustrate.py
docker compose exec app ruff format --check server/app/modules/pipelines/nodes/ai_illustrate.py
```

如有 F401 unused import，删；format 改写。

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/pipelines/nodes/ai_illustrate.py server/tests/test_pipeline_ai_illustrate.py
git commit -m "$(cat <<'EOF'
refactor(pipelines): ai_illustrate 节点改用 ai_illustrate_svc.illustrate_one

删 _format_one + _maybe_set_cover + _collect_format_errors 三个内部 fn
（约 -55 行），_one() 改 1 行调 service；聚合循环消费 IllustrateResult。
NodeResult.output 6 字段（article_ids/errors/images_inserted/format_errors/
covers_set/cover_errors）完全不变 —— snapshot 测试守住对外契约。

为 Task 3 新 MCP endpoint 共用同一份配图实现做准备：两条路径效果完全一致。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 新 MCP endpoint `POST /api/articles/{id}/ai-illustrate`

**Files:**
- Modify: `server/app/modules/articles/router.py`（在 `illustrate_article_mcp` 之后追加）
- Test: `server/tests/test_articles_ai_illustrate_endpoint.py`（新建）

- [ ] **Step 1: 写失败的鉴权 + 集成测试**

创建 `server/tests/test_articles_ai_illustrate_endpoint.py`：

```python
"""POST /api/articles/{id}/ai-illustrate MCP endpoint 鉴权 + 调度集成测试.

mock service 层 illustrate_one，只测 endpoint 把 payload 正确翻成
IllustrateOptions + 返回 IllustrateResult 的字段映射.
"""

from __future__ import annotations

import pytest

from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_ai_illustrate_endpoint_requires_mcp_token(monkeypatch):
    """不带 X-MCP-Token → 401."""
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        r = test_app.client.post(
            "/api/articles/1/ai-illustrate",
            json={"main_category_id": 1},
        )
        assert r.status_code == 401
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_ai_illustrate_endpoint_returns_result_when_authed(monkeypatch):
    """带 token + mock service 返指定 IllustrateResult → 响应 4 字段完整映射."""
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        from server.app.modules.articles.ai_illustrate_svc import IllustrateResult

        called: dict = {}

        def fake_illustrate_one(*, article_id, main_category_id, user_id, options, session_factory):
            called["article_id"] = article_id
            called["main_category_id"] = main_category_id
            called["set_cover"] = options.set_cover
            called["include_companion"] = options.include_companion
            return IllustrateResult(
                article_id=article_id,
                images_inserted=5,
                cover_status="set",
                cover_error=None,
                format_error=None,
            )

        monkeypatch.setattr(
            "server.app.modules.articles.router.illustrate_one", fake_illustrate_one
        )

        r = test_app.client.post(
            "/api/articles/123/ai-illustrate",
            json={
                "main_category_id": 42,
                "include_companion": False,
                "set_cover": True,
            },
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["images_inserted"] == 5
        assert body["cover_status"] == "set"
        assert body["cover_error"] is None
        assert body["format_error"] is None
        assert called["article_id"] == 123
        assert called["main_category_id"] == 42
        assert called["include_companion"] is False
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 跑测试，确认 2 个 fail**

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_articles_ai_illustrate_endpoint.py -q
```

**预期**：fail with 404（endpoint 还不存在）或 monkeypatch import error。

- [ ] **Step 3: 加 endpoint + Pydantic models + helper**

修改 `server/app/modules/articles/router.py`：

(a) 顶部 import 区追加（如果还没 import）：

```python
import os

from server.app.modules.articles.ai_illustrate_svc import (
    IllustrateOptions,
    illustrate_one,
)
```

(b) 顶部 module 级常量区追加（紧邻其它 module 常量）：

```python
# MCP 路径下没有 user JWT，跟 save_from_mcp 同款用环境变量常量
_MCP_OPERATOR_USER_ID = int(os.environ.get("GEO_MCP_OPERATOR_USER_ID", "1"))
```

(c) 在 `illustrate_article_mcp`（约 line 979-1031）之后追加：

```python
class AiIllustratePayload(BaseModel):
    """走 ai_illustrate 节点同款逻辑（AI 决策 + 自动封面）."""

    main_category_id: int
    include_companion: bool = True
    web_fallback: bool = False
    aggressive_images: bool = True
    max_images: int | None = Field(default=None, ge=1, le=50)
    min_spacing: int | None = Field(default=None, ge=1, le=20)
    preset_id: int | None = None
    set_cover: bool = True


class AiIllustrateResponse(BaseModel):
    images_inserted: int
    cover_status: str
    cover_error: str | None
    format_error: str | None


@articles_mcp_router.post(
    "/{article_id}/ai-illustrate",
    response_model=AiIllustrateResponse,
    dependencies=[Depends(require_mcp_token)],
)
def ai_illustrate_article_mcp(
    article_id: int,
    payload: AiIllustratePayload,
) -> AiIllustrateResponse:
    """[MCP] AI 智能配图 + 自动封面，对齐 Web UI「AI 配图」pipeline 节点.

    复用 articles.ai_illustrate_svc.illustrate_one；与 pipeline 节点共享同一份实现.
    """
    from server.app.db.session import SessionLocal

    result = illustrate_one(
        article_id=article_id,
        main_category_id=payload.main_category_id,
        user_id=_MCP_OPERATOR_USER_ID,
        options=IllustrateOptions(
            include_companion=payload.include_companion,
            web_fallback=payload.web_fallback,
            aggressive_images=payload.aggressive_images,
            max_images=payload.max_images,
            min_spacing=payload.min_spacing,
            preset_id=payload.preset_id,
            set_cover=payload.set_cover,
        ),
        session_factory=SessionLocal,
    )
    return AiIllustrateResponse(
        images_inserted=result.images_inserted,
        cover_status=result.cover_status,
        cover_error=result.cover_error,
        format_error=result.format_error,
    )
```

- [ ] **Step 4: 跑测试，确认 2 pass**

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_articles_ai_illustrate_endpoint.py -q
```

**预期**：`2 passed`。

- [ ] **Step 5: ruff + format clean**

```bash
docker compose exec app ruff check server/app/modules/articles/router.py server/tests/test_articles_ai_illustrate_endpoint.py
docker compose exec app ruff format --check server/app/modules/articles/router.py server/tests/test_articles_ai_illustrate_endpoint.py
```

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/articles/router.py server/tests/test_articles_ai_illustrate_endpoint.py
git commit -m "$(cat <<'EOF'
feat(articles): POST /api/articles/{id}/ai-illustrate MCP endpoint + 2 鉴权 / 调度测试

新 MCP endpoint 包 ai_illustrate_svc.illustrate_one；rooter-level
require_mcp_token；Payload 8 字段对应 IllustrateOptions（默认值与 Web UI
ai_illustrate 节点 cfg 一致）；Response 4 字段平铺 IllustrateResult。

旧 illustrate_article_mcp endpoint 保留兼容（generation-loop.md 引用），
不删——未来 v3 清。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: 新 MCP 工具 `ai_illustrate_article`

**Files:**
- Modify: `server/mcp/tools/action.py`（末尾追加工具）

无自动测——工具是 `_apost` 薄壳，端到端鉴权 + 行为已由 Task 3 的 endpoint 测试覆盖。

- [ ] **Step 1: 在 action.py 末尾追加新工具**

```python
@mcp.tool()
async def ai_illustrate_article(
    article_id: int,
    main_category_id: int,
    include_companion: bool = True,
    aggressive_images: bool = True,
    set_cover: bool = True,
) -> dict[str, Any]:
    """AI-driven illustration + auto cover for one article (Web UI parity).

    Uses GEO's run_ai_format under the hood — the AI model picks which images
    to insert and where, based on article content. Draws images from
    main_category_id + (optionally) all companion categories. Auto-sets cover
    from main_category_id if article has no cover.

    Args:
        article_id: Target article (must exist).
        main_category_id: Stock image library category id ("主推栏目"). The matrix
            section in your writer SKILL.md tells you which id to use.
        include_companion: If True, also draws from all companion categories.
            Default True matches Web UI default.
        aggressive_images: If True, "积极" style (more images, less spacing).
            Default True matches Web UI default.
        set_cover: If True, also picks a random image from main_category_id
            as the article cover (only if cover not already set).

    Returns:
        {"ok": True, "data": {
            "images_inserted": int,
            "cover_status": str,        # "set" | "skipped_existing" | "no_image" | "error" | "skipped"
            "cover_error": str | None,
            "format_error": str | None,
        }, "error": None}
    """
    body: dict[str, Any] = {
        "main_category_id": main_category_id,
        "include_companion": include_companion,
        "aggressive_images": aggressive_images,
        "set_cover": set_cover,
    }
    return await _apost(f"/api/articles/{article_id}/ai-illustrate", json=body)
```

- [ ] **Step 2: ruff + import 自检**

```bash
docker compose exec app ruff check server/mcp/tools/action.py
docker compose exec app ruff format --check server/mcp/tools/action.py
docker compose exec app python -c "import server.mcp.tools.action; print('ok')"
```

**预期**：`ok`。

- [ ] **Step 3: 确认 MCP 工具数 +1**

```bash
docker compose exec app python -c "from server.mcp.server import mcp; print('tools count:', len(mcp._tool_manager._tools))"
```

**预期**：原来 19 → 现在 **20**。

- [ ] **Step 4: 同步 MCP_TOOLS_COUNT 到 20 + 更新 test_mcp_connect 断言**

修改 `server/app/modules/mcp_catalog/connect_router.py`：

```python
MCP_TOOLS_COUNT = 20   # was 19
```

修改 `server/tests/test_mcp_connect.py`（test_status_returns_configured_true_when_token_set 那个断言）：

```python
        assert body["tools_count"] == 20   # was 19
```

跑测试确认：

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_mcp_connect.py -q
```

**预期**：`3 passed`。

- [ ] **Step 5: Commit**

```bash
git add server/mcp/tools/action.py server/app/modules/mcp_catalog/connect_router.py server/tests/test_mcp_connect.py
git commit -m "$(cat <<'EOF'
feat(mcp): ai_illustrate_article 工具 — /goal Loop 配图对齐 Web UI

action 组从 7 个工具增到 8 个。async + _apost 薄壳，转发到后端
/api/articles/{id}/ai-illustrate；只暴露 4 个常用旋钮（main_category_id
+ include_companion + aggressive_images + set_cover），其余 advanced
参数走 endpoint 默认。

MCP_TOOLS_COUNT 19→20 同步 + test_mcp_connect 断言同步。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: writer + orchestrator skill 模板 + README onboarding

**Files:**
- Modify: `server/app/modules/loop_skills/templates/skills/geo-article-writer/SKILL.md`
- Modify: `server/app/modules/loop_skills/templates/skills/geo-goal-orchestrator/SKILL.md`
- Modify: `server/app/modules/loop_skills/templates/README.md`

- [ ] **Step 1: 改 writer skill「Required Checklist」step 5**

修改 `server/app/modules/loop_skills/templates/skills/geo-article-writer/SKILL.md`：

把这一行：

```markdown
5. `illustrate_article(article_id)` — best-effort，失败吞掉
```

替换为：

```markdown
5. `ai_illustrate_article(article_id, main_category_id=<从矩阵特例段拿>)` —
   AI 智能配图 + 自动封面，**返回值检查 `format_error` / `cover_error` 字段**；
   有错就在最后 JSON 里加 `illustration_warnings` 透传给 orchestrator，不抛错
```

- [ ] **Step 2: 改 writer skill「矩阵特例」段**

同一文件，把这段：

```markdown
## 矩阵特例：餐厅养成记官方矩阵（默认）

- 风格：轻松实用，避免「开篇一段宏大引入」，直接进主题
- 偏好题材：游戏推荐 / 攻略 / 玩法解析 / 国风游戏综述
- 配图类别：温馨治愈、国风山水（具体 stock_category_id 让 `illustrate_article` 自动按文章 tag 选；不要写死 category_ids）
```

替换为：

```markdown
## 矩阵特例：餐厅养成记官方矩阵（默认）

- 风格：轻松实用，避免「开篇一段宏大引入」，直接进主题
- 偏好题材：游戏推荐 / 攻略 / 玩法解析 / 国风游戏综述
- 配图主推栏目：`main_category_id = <REPLACE_ME>`  # ← 安装时查 GEO 后台
  「图库管理」→ 主推栏目「餐厅养成记」的 id；写死在这里
- 配图风格：默认 `aggressive_images=True`（积极配图，每个明确出现的游戏都插）
- 封面：默认 `set_cover=True`（从主推栏目随机取一张做封面，已有封面则跳过）
- 陪衬：默认 `include_companion=True`（AI 同时从所有陪衬栏目选）

> 调用约定：
> `ai_illustrate_article(article_id=<>, main_category_id=<上面那个值>)`
> 其余 3 个布尔参数走默认即可。
```

- [ ] **Step 3: 改 orchestrator skill 进度日志 6 行（中文化）**

修改 `server/app/modules/loop_skills/templates/skills/geo-goal-orchestrator/SKILL.md`：

找到 "# 进度日志（必须 echo 这些短行）" 段，把这块代码块：

```
[orchestrator] sanity ✓ pool=<name> N=<N> matrix=<code|default>
[round k/3N] qid=<id> → writer …
[round k/3N] writer 交稿 article_id=<id>, verifier …
[round k/3N] verifier decision=<d> score=<total>
[netto] today approved by goal-verifier: <count>/<N>
[done|abort] 净产出 <count>/<N>, 共耗时 <m>m, 原因=<...>
```

替换为：

```
[快检] pool=<name> N=<N> matrix=<code|默认> 通过
[第 k/3N 轮] 选题 qid=<id> → 改写中 …
[第 k/3N 轮] 改写完成 article_id=<id>, 评审中 …
[第 k/3N 轮] 评审 决策=<d> 总分=<total>
[净产出] 今日通过 goal 评审的文章数: <count>/<N>
[完成|中止] 净产出 <count>/<N>, 共耗时 <m>m, 原因=<...>
```

- [ ] **Step 4: README onboarding 加 step 6**

修改 `server/app/modules/loop_skills/templates/README.md`，找到 "## 第一次用 /goal —— 5 步 onboarding" 那个代码块，把整块替换为：

```markdown
## 第一次用 /goal —— 6 步 onboarding

\`\`\`
1. 把 spec/plan 里的 SKILL.md + command + README 内容复制到本地 .claude/
   （或从同事那拿 zip / 用 install_loop_skills MCP 工具自动装）
2. 一次性配置（每台机器一次）
   - 打开 ~/.claude.json，加 mcpServers.geo 段
   - 把后端管理员发的 GEO_MCP_TOKEN 填到 headers.X-MCP-Token
   - 详细参考 docs/mcp-setup-notes.md
3. 重启 Claude Code
4. 在 Claude Code 里输入 /mcp，确认 geo server 显示 "connected"
5. 打开本机 .claude/skills/geo-article-writer/SKILL.md，找到「矩阵特例」段
   `main_category_id = <REPLACE_ME>` 行；去 GEO 后台「图库管理」→ 主推栏目
   里找你矩阵对应栏目（比如餐厅养成记），把 id 填进去（数字）。
6. 在 Claude Code 里输入：
   /goal 帮我今天产出 5 篇关于国风游戏的文章

之后 /goal 会自动跑（约 10-20 分钟）；完成后飞书群会有播报。
\`\`\`
```

> 注意：上面 `\`\`\`` 是 escape，实际写入文件时是字面的三反引号 ` ``` `。

- [ ] **Step 5: 确认 3 个模板文件改完且 markdown 格式正确**

```bash
docker compose exec app python -c "
from pathlib import Path
tpl = Path('server/app/modules/loop_skills/templates')
for p in ['skills/geo-article-writer/SKILL.md', 'skills/geo-goal-orchestrator/SKILL.md', 'README.md']:
    text = (tpl / p).read_text(encoding='utf-8')
    # 关键 string 存在性 sanity check
    if p.endswith('writer/SKILL.md'):
        assert 'ai_illustrate_article(article_id' in text, f'{p}: writer step 5 not updated'
        assert 'main_category_id = <REPLACE_ME>' in text, f'{p}: matrix not updated'
    if p.endswith('orchestrator/SKILL.md'):
        assert '[第 k/3N 轮]' in text, f'{p}: log lines not zhCN'
        assert '[netto]' not in text, f'{p}: old [netto] tag still present'
    if p == 'README.md':
        assert '6 步 onboarding' in text, f'{p}: onboarding not updated'
        assert 'main_category_id = <REPLACE_ME>' in text, f'{p}: step 5 about category id missing'
print('all template sanity checks passed')
"
```

**预期**：`all template sanity checks passed`。如有 AssertionError，回去补漏。

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/loop_skills/templates/
git commit -m "$(cat <<'EOF'
feat(loop_skills/templates): writer 改调 ai_illustrate_article + orchestrator 日志中文化

3 处改：
- writer SKILL.md step 5：从 illustrate_article 改成 ai_illustrate_article
  + 检查 format_error/cover_error 返回值；矩阵特例段加 main_category_id
  写死位（<REPLACE_ME>）+ 4 个默认旋钮说明
- orchestrator SKILL.md 进度日志 6 行从英文混搭（sanity/round/writer/
  verifier/netto/today approved by goal-verifier）改成全中文（[快检]/
  [第 k/3N 轮]/[净产出]/[完成|中止]），保留方括号 tag + 英文技术标识符
- templates README 5 步 onboarding 改成 6 步，加首次填 main_category_id
  的明确步骤 + 提到 install_loop_skills 自动装路径

下一步 Task 6 bump bundle version v1→v2，让分发链路把模板更新推到所有
使用者本机。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: bundle version bump v1→v2

**Files:**
- Modify: `server/app/modules/loop_skills/version.py`

- [ ] **Step 1: 跑 build_bundle 拿当前 sha**

```bash
docker compose exec app python -c "from server.app.modules.loop_skills.service import build_bundle; print(build_bundle().bundle_sha256)"
```

**预期**：打出一串 64 字符 hex。**复制这串**到下一步。

- [ ] **Step 2: 跑 sha 校验测试，确认 fail**

```bash
docker compose exec app pytest server/tests/test_loop_skill_bundle.py::test_bundle_sha_is_known -v
```

**预期**：fail with `Bundle sha256 = '<step 1 拿到的 sha>' not in KNOWN_BUNDLE_SHAS`。

- [ ] **Step 3: bump version + 加 sha 到 KNOWN**

修改 `server/app/modules/loop_skills/version.py`：

```python
"""手工维护的 bundle 版本号 + 已审核 sha 集合.

CI 测试 (test_loop_skill_bundle.py::test_bundle_sha_is_known) 会校验：
如果 build_bundle().bundle_sha256 没记录在 KNOWN_BUNDLE_SHAS 集合里，
fail + 提示开发者：把新 sha 加进 KNOWN_BUNDLE_SHAS 并 bump
LOOP_SKILL_BUNDLE_VERSION，强制「改模板必同步 bump 版本」纪律.
"""

LOOP_SKILL_BUNDLE_VERSION = "2026-06-25-v2"

KNOWN_BUNDLE_SHAS: frozenset[str] = frozenset({
    "49f824a36606c285b84c71ade5aec406ea3c545599b7a3ccf59f863b521667dd",  # v1 (2026-06-24)
    "<把 step 1 打出的 sha 串填到这里，64 字符 hex>",  # v2 (2026-06-25)
})
```

把 `<把 step 1 打出的 sha 串填到这里>` 替换为 Step 1 实际打印出的 sha。

- [ ] **Step 4: 跑 bundle 全部测试，确认 9 pass**

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test app pytest server/tests/test_loop_skill_bundle.py -q
```

**预期**：`9 passed`（其中 test_bundle_sha_is_known 现在通过）。

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/loop_skills/version.py
git commit -m "$(cat <<'EOF'
chore(loop_skills): bump LOOP_SKILL_BUNDLE_VERSION v1→v2 + 加 v2 sha 到 KNOWN

Task 5 改了 3 个模板文件，bundle sha 自然变。v1 sha 保留：
已经装了 v1 的使用者本机 .claude/ 里就是 v1 内容；他们升级前 KNOWN
仍要认这个 sha。Web Section ⑤ 的 /info 端点会返回当前 v2 + 新 sha，
使用者比对自己本机版本判断是否要重装。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: 后端全 lint/test + push + PR

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

**预期**：全 pass。本次新增 6 + 1 + 2 = 9 个测试，加上 test_mcp_connect 已有 3 个断言更新。

- [ ] **Step 3: 前端 typecheck（无改动应自然过）**

```bash
pnpm --filter @geo/web typecheck
```

**预期**：0 error（前端零改动）。

- [ ] **Step 4: 推分支**

```bash
git push -u origin fix/loop-illustration-and-i18n
```

- [ ] **Step 5: 建 PR**

```bash
gh pr create --title "fix(loop): /goal 生文 Loop 配图对齐 Web UI + 进度日志中文化" --body "$(cat <<'EOF'
## Summary

修两个 /goal Loop 的 bug：

1. **配图缺失**：MCP loop 一直在调简陋的 \`illustrate_article_mcp\`（按位置 [2,4,6] 硬塞图、无 AI 决策、要 stock_categories、不设封面）。新方案抽取共享 service \`articles/ai_illustrate_svc.py\`，让 pipeline \`ai_illustrate\` 节点 + 新 MCP endpoint \`POST /api/articles/{id}/ai-illustrate\` 都调它——保证 MCP 路径配图效果与 Web UI 完全一致（AI 智能选图 + 主推+陪衬栏目 + 自动封面 + 错误暴露）。
2. **进度日志英文混搭**：orchestrator skill 6 行从英文混搭改全中文，保留方括号 tag + 英文技术标识符（pool/qid/article_id 等）；subagent prompts 保持英文不动（技术契约）。

bump bundle version v1→v2，v1 sha 保留供本地未升级用户的 KNOWN 校验。
MCP_TOOLS_COUNT 19→20。

## Test plan

- [x] 后端 ruff / format / pytest 全过（CI 门禁）
- [x] 9 个新单测通过（6 service + 1 pipeline snapshot + 2 endpoint 鉴权/调度）
- [x] test_mcp_connect 工具数断言 19→20 同步
- [x] 前端 typecheck 通过（前端零改动）
- [ ] **使用者本地装 v2 + 填 main_category_id 后跑 \`/goal 1 篇国风游戏文章作为冒烟\`**：进度日志全中文 + 文章有插图 + 有封面（user 手动验证）
- [ ] **同题材跑 Web UI 「方案运行」一篇做参照**：配图效果视觉一致（user 手动验证）

## 设计 / 实施

- 设计稿：\`docs/superpowers/specs/2026-06-25-loop-illustration-and-i18n-fix-design.md\`
- 实施 plan：\`docs/superpowers/plans/2026-06-25-loop-illustration-and-i18n-fix.md\`
- 上游 PR：#144 (已合) + #147 (已合)

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
| §1 问题定位（配图 + i18n） | Task 1-5 整体解决 |
| §2 锁定决策 4 项 | 全部体现在 Task 1-5 实施细节 |
| §3 架构总览（service 抽取 + 2 路径 + 6 文件） | Task 1 (service) + Task 2 (pipeline) + Task 3 (endpoint) + Task 4 (MCP 工具) + Task 5 (skill 模板) |
| §4.1 service.py 完整代码 | Task 1 Step 3 |
| §4.2 pipeline 节点改 | Task 2 Step 3 |
| §4.3 endpoint 代码 | Task 3 Step 3 |
| §4.4 MCP 工具签名 | Task 4 Step 1 |
| §5.1 writer skill 改 | Task 5 Step 1+2 |
| §5.2 orchestrator skill 6 行 | Task 5 Step 3 |
| §5.3 README onboarding | Task 5 Step 4 |
| §6 version bump | Task 6 |
| §7 失败矩阵 + 不变式 | Task 1 的 6 个测试覆盖 8 种失败矩阵中的关键场景；Task 2 snapshot 守住 pipeline schema 不变式 |
| §8.1 自动测 9+1 | Task 1 (6) + Task 2 (1) + Task 3 (2) + Task 6 (sha 校验已有，自动通过) |
| §8.2 手工冒烟 6 步 | Task 7 PR description 里 Test plan 列出（unchecked 由 user 验证） |
| §9 工作量 + 顺序 | Plan task 顺序就是 §9.2 |
| §10 与已合 PR 关系 | Plan Architecture + Branch 段引用 |
| §11 Out of Scope | 隐含落实（不出现在 task 列表里） |
| §12 上线门禁 | Task 7 PR description Test plan |

**结论：全覆盖**。

**2. Placeholder scan** — 检查无 TBD / TODO / "implement later"：
- Task 6 Step 3 的 `<把 step 1 打出的 sha 串填到这里>` 是**有意运行时占位**（必须实际跑 build_bundle 拿到 sha 才知道），文档清楚说明替换流程 ✓
- writer skill 矩阵段的 `<REPLACE_ME>` 是**给最终使用者填的占位**，README onboarding step 5 明确告诉他们怎么填 ✓
- 其它无遗留占位

**3. Type consistency**
- `IllustrateOptions(include_companion, web_fallback, aggressive_images, max_images, min_spacing, preset_id, set_cover)` — Task 1 定义、Task 2 + Task 3 调用，字段名一致
- `IllustrateResult(article_id, images_inserted, cover_status, cover_error, format_error)` — 同上
- `illustrate_one(*, article_id, main_category_id, user_id, options, session_factory)` — Task 1 定义，Task 2 (pipeline) + Task 3 (endpoint) 调用，关键字参数名一致
- `AiIllustratePayload.main_category_id` (后端) ↔ `ai_illustrate_article(..., main_category_id)` (MCP 工具) ↔ writer skill `main_category_id` (调用约定) —— 三处命名一致
- `LOOP_SKILL_BUNDLE_VERSION = "2026-06-25-v2"` — Task 6
- `MCP_TOOLS_COUNT = 20` — Task 4 (从 19 bump)

**结论：一致**。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-25-loop-illustration-and-i18n-fix.md`. Two execution options:

**1. Subagent-Driven (recommended)** — 我每个 task 起 fresh subagent 跑，task 之间 review，迭代快。

**2. Inline Execution** — 我在当前会话里逐 task 顺序执行，每 2-3 个 task 检查一次。

Which approach?
