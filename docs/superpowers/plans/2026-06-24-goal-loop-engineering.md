# `/goal` Loop Engineering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把生文 Loop 从「主对话顺序写 N 篇 + writer 自评」升级到「`/goal` 编排 + Ralph 风格独立 fresh-context writer/verifier subagents + 净产出验证作停止条件」。

**Architecture:** 项目级 `.claude/` 目录承载 slash command + 3 个 SKILL.md（orchestrator / writer / verifier 各一），主对话装载 orchestrator skill 后调度 Agent 工具 spawn 子 agent；新增一个 MCP 工具 `list_today_loop_articles` 让主对话查 GEO 数据库拿 ground truth 决定是否继续循环。沿用 [`2026-06-18-claude-code-loop-with-geo-mcp-design.md`](../specs/2026-06-18-claude-code-loop-with-geo-mcp-design.md) 已经落地的 MCP server 架构、鉴权、飞书通道，不动 17 个现有 tools。

**Tech Stack:** FastMCP (Python) / FastAPI / SQLAlchemy 2.x ORM / pytest `@pytest.mark.mysql` / Claude Code slash commands + skills

**Spec:** [`docs/superpowers/specs/2026-06-24-goal-loop-engineering-design.md`](../specs/2026-06-24-goal-loop-engineering-design.md)

**Branch:** `docs/goal-loop-engineering`（已从 `origin/main` 拉出，spec 已 commit 在 `4d0f79f`）

---

## 🟡 PR 范围调整（重要）

执行过程中确定的分发模型：**`.claude/` 不入 git**，每位使用者本地各自维护一份。

落到本 plan：

| Task | 是否入 PR | 说明 |
|---|---|---|
| Tasks 1, 2, 3 | ✅ 入 PR | 后端 service / endpoint / MCP 工具 |
| Task 4 (skill lint 测试) | ❌ 跳过 | `.claude/` 不在 CI checkout 范围内，写 lint 测 CI 必失败 |
| Tasks 5, 6, 7 (3 个 SKILL.md body) | 📋 仅作 reference | 完整内容仍保留在本 plan 里供本地 copy；不入 git |
| Task 8 (`/goal` slash command) | 📋 仅作 reference | 同上 |
| Task 9 (`.claude/README.md`) | 📋 仅作 reference | 同上 |
| Task 10 (lint + 全测 + PR) | ✅ 入 PR，但范围只覆盖后端 | 手工冒烟由使用者本地跑 |

执行历史：Tasks 1-9 在 Subagent-Driven 执行下已全部 done 并 commit 过，
随后按本新决策 `git reset --hard` 到 Task 3 之后状态，丢掉 Tasks 4-9 的
6 个 commits。Tasks 5-9 的内容是已验证过可工作的版本，使用者照抄安全。

---

## Files to Touch

**入库**：

| 文件 | 操作 | 责任 |
|---|---|---|
| `server/app/modules/auto_review/service.py` | 追加 `list_recent_decisions(...)` | DB 查询：滚动时间窗内的 `AutoReviewDecision` join `Article`，可选按 `metrics.writer_model` 过滤 |
| `server/app/modules/auto_review/router.py` | 追加 `GET /today-loop-decisions` 端点 | HTTP 入口；走 `Depends(require_mcp_token)`；异常包 `mcp_exception_response` |
| `server/mcp/tools/catalog.py` | 追加 `list_today_loop_articles` async 工具 | MCP 协议层薄壳，转发到 `/api/articles/today-loop-decisions` |
| `server/tests/test_auto_review_loop_query.py` | 新建 | service 单测 + 端点鉴权测试 |

**本地不入库（各使用者自维护）**：

| 文件 | 操作 | 责任 |
|---|---|---|
| `.claude/skills/geo-goal-orchestrator/SKILL.md` | 本地新建 | 主对话调度 playbook（内容见 Task 7） |
| `.claude/skills/geo-article-writer/SKILL.md` | 本地新建 | writer subagent playbook（内容见 Task 5） |
| `.claude/skills/geo-article-verifier/SKILL.md` | 本地新建 | verifier subagent playbook（内容见 Task 6） |
| `.claude/commands/goal.md` | 本地新建 | `/goal` slash command 入口（内容见 Task 8） |
| `.claude/README.md` | 本地新建 | 同事 onboarding 入口（内容见 Task 9） |

**关键边界**：

- `service.py` 只写**纯查询**——返回 `(int, list[dict])`，不抛业务异常、不 commit。
- `router.py` 只做**协议 + 鉴权 + 异常包装**——不写业务逻辑。
- `catalog.py` 是**MCP 协议层薄壳**——只负责把参数 clip 到允许范围 + 调 `_aget`。
- 3 个 SKILL.md **不互相 import**——每个都自描述，subagent 独立 fresh context 装载。
- `goal.md` 只做**一件事**：load orchestrator skill 然后 echo "Skill loaded, follow it."；具体调度逻辑在 skill 里。

---

## Task 1: 后端 `list_recent_decisions` service 函数（TDD）

**Files:**
- Test: `server/tests/test_auto_review_loop_query.py`（新建）
- Modify: `server/app/modules/auto_review/service.py`（在文件末尾追加函数 + 顶部 import）

- [ ] **Step 1: 写失败的测试**

创建 `server/tests/test_auto_review_loop_query.py`：

```python
"""list_recent_decisions service 测试 + /today-loop-decisions 端点测试。

测试覆盖：
- 基本查询（命中 decided_by + decision）
- 时间窗边界（since_hours 之外不算）
- decided_by 过滤（其它 decided_by 排除）
- model_label 过滤（Article.metrics.writer_model）
- limit 截断 items 但 count 给全量
- 端点鉴权（无 MCP token → 401）
- 端点正常返回（count + items 结构）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from server.tests.utils import build_test_app


def _mk_article(test_app, *, title: str, writer_model: str | None = None) -> int:
    """Helper: 建一篇最小 article，可选写 metrics.writer_model。"""
    from server.app.modules.articles.models import Article

    db = test_app.session_factory()
    try:
        a = Article(
            user_id=test_app.admin_id,
            title=title,
            content_json=json.dumps({"type": "doc", "content": []}),
            content_html="",
            plain_text="",
            word_count=0,
            status="draft",
            review_status="pending",
            metrics={"writer_model": writer_model} if writer_model else None,
        )
        db.add(a)
        db.commit()
        return a.id
    finally:
        db.close()


def _mk_decision(
    test_app,
    *,
    article_id: int,
    decided_by: str = "claude-goal-verifier",
    decision: str = "approved",
    score_total: int | None = 80,
    created_at: datetime | None = None,
) -> int:
    """Helper: 建一条 AutoReviewDecision，可选指定 created_at（用于时间窗测试）。"""
    from server.app.modules.auto_review.models import AutoReviewDecision

    db = test_app.session_factory()
    try:
        d = AutoReviewDecision(
            article_id=article_id,
            decision=decision,
            score_total=score_total,
            score_breakdown=None,
            reasoning=None,
            decided_by=decided_by,
        )
        if created_at is not None:
            d.created_at = created_at
        db.add(d)
        db.commit()
        return d.id
    finally:
        db.close()


@pytest.mark.mysql
def test_list_recent_decisions_basic(monkeypatch):
    """命中 decided_by + decision 的行进入 count & items。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.auto_review.service import list_recent_decisions

        a1 = _mk_article(test_app, title="goal-article-1")
        a2 = _mk_article(test_app, title="goal-article-2")
        a3 = _mk_article(test_app, title="other-article")

        _mk_decision(test_app, article_id=a1)  # claude-goal-verifier / approved
        _mk_decision(test_app, article_id=a2)  # claude-goal-verifier / approved
        _mk_decision(test_app, article_id=a3, decided_by="other-bot")  # 不命中

        db = test_app.session_factory()
        try:
            count, items = list_recent_decisions(
                db,
                decided_by="claude-goal-verifier",
                decision="approved",
                since_hours=24,
            )
            assert count == 2
            assert {it["title"] for it in items} == {"goal-article-1", "goal-article-2"}
            assert all("decided_at" in it for it in items)
            assert all(it["score_total"] == 80 for it in items)
        finally:
            db.close()
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_list_recent_decisions_time_window(monkeypatch):
    """26h 前的 decision 不算（since_hours=24 默认）。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.auto_review.service import list_recent_decisions

        a_old = _mk_article(test_app, title="old")
        a_new = _mk_article(test_app, title="new")

        _mk_decision(test_app, article_id=a_old, created_at=datetime.utcnow() - timedelta(hours=26))
        _mk_decision(test_app, article_id=a_new)  # 默认 utcnow

        db = test_app.session_factory()
        try:
            count, items = list_recent_decisions(
                db,
                decided_by="claude-goal-verifier",
                decision="approved",
                since_hours=24,
            )
            assert count == 1
            assert items[0]["title"] == "new"
        finally:
            db.close()
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_list_recent_decisions_decision_filter(monkeypatch):
    """decision='approved' 不命中 needs_rewrite / rejected 行。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.auto_review.service import list_recent_decisions

        a1 = _mk_article(test_app, title="ok")
        a2 = _mk_article(test_app, title="rw")

        _mk_decision(test_app, article_id=a1, decision="approved")
        _mk_decision(test_app, article_id=a2, decision="needs_rewrite")

        db = test_app.session_factory()
        try:
            count, items = list_recent_decisions(
                db,
                decided_by="claude-goal-verifier",
                decision="approved",
                since_hours=24,
            )
            assert count == 1
            assert items[0]["title"] == "ok"
        finally:
            db.close()
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_list_recent_decisions_model_label_filter(monkeypatch):
    """model_label='X' 只命中 article.metrics.writer_model == 'X' 的行。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.auto_review.service import list_recent_decisions

        a1 = _mk_article(test_app, title="goal-loop", writer_model="claude-goal-opus-4-7")
        a2 = _mk_article(test_app, title="other-loop", writer_model="claude-other")
        a3 = _mk_article(test_app, title="no-label")

        _mk_decision(test_app, article_id=a1)
        _mk_decision(test_app, article_id=a2)
        _mk_decision(test_app, article_id=a3)

        db = test_app.session_factory()
        try:
            count, items = list_recent_decisions(
                db,
                decided_by="claude-goal-verifier",
                decision="approved",
                since_hours=24,
                model_label="claude-goal-opus-4-7",
            )
            assert count == 1
            assert items[0]["title"] == "goal-loop"
        finally:
            db.close()
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_list_recent_decisions_limit_caps_items_not_count(monkeypatch):
    """limit=2 但匹配 5 行：count=5、items 长度 2。"""
    test_app = build_test_app(monkeypatch)
    try:
        from server.app.modules.auto_review.service import list_recent_decisions

        for i in range(5):
            aid = _mk_article(test_app, title=f"a{i}")
            _mk_decision(test_app, article_id=aid)

        db = test_app.session_factory()
        try:
            count, items = list_recent_decisions(
                db,
                decided_by="claude-goal-verifier",
                decision="approved",
                since_hours=24,
                limit=2,
            )
            assert count == 5
            assert len(items) == 2
        finally:
            db.close()
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 跑测试，确认失败**

在 dev 容器里跑（用 [memory: pytest-dev-container-env](../../../C:/Users/admin/.claude/projects/C--Users-admin-Desktop-geo-collab/memory/pytest-dev-container-env.md) 提到的环境变量）：

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@db:3306/geo_test \
  app pytest server/tests/test_auto_review_loop_query.py -q
```

**预期**：5 个测试**都 fail**，错误形如 `ImportError: cannot import name 'list_recent_decisions' from 'server.app.modules.auto_review.service'`。

- [ ] **Step 3: 在 service.py 顶部加 import**

修改 `server/app/modules/auto_review/service.py`，顶部 import 区追加：

```python
from datetime import datetime, timedelta

from sqlalchemy import func

from server.app.modules.articles.models import Article
```

注意 `Article` 可能已经间接 import，但显式导入更清楚。`datetime` / `timedelta` / `func` 是新增依赖。

- [ ] **Step 4: 实现 `list_recent_decisions` 函数**

追加到 `server/app/modules/auto_review/service.py` **末尾**：

```python
def list_recent_decisions(
    db: Session,
    *,
    decided_by: str,
    decision: str,
    since_hours: int,
    model_label: str | None = None,
    limit: int = 50,
) -> tuple[int, list[dict]]:
    """返回 (total_count, items[:limit])。

    items: [{article_id, title, decided_at, score_total}], newest first.
    total_count 是滚动时间窗内全部命中行数（不被 limit 影响）。

    用于 `/goal` orchestrator 的净产出验证 —— 主对话每轮调一次拿 ground truth
    决定是否继续循环。

    Args:
        decided_by: AutoReviewDecision.decided_by 精确匹配。
        decision: AutoReviewDecision.decision 精确匹配。
        since_hours: 滚动时间窗（小时），从当前 UTC 时间往回数。
        model_label: 可选，进一步要求 Article.metrics.writer_model 等于此值。
            None 表示不过滤这个维度。
        limit: items 数组的截断上限；count 不受影响。
    """
    since = datetime.utcnow() - timedelta(hours=since_hours)

    q = (
        db.query(AutoReviewDecision, Article)
        .join(Article, Article.id == AutoReviewDecision.article_id)
        .filter(
            AutoReviewDecision.decided_by == decided_by,
            AutoReviewDecision.decision == decision,
            AutoReviewDecision.created_at >= since,
        )
    )
    if model_label:
        q = q.filter(
            func.json_unquote(func.json_extract(Article.metrics, "$.writer_model"))
            == model_label
        )

    total = q.count()
    rows = q.order_by(AutoReviewDecision.created_at.desc()).limit(limit).all()
    items = [
        {
            "article_id": a.id,
            "title": a.title,
            "decided_at": d.created_at.isoformat() + "Z",
            "score_total": d.score_total,
        }
        for d, a in rows
    ]
    return total, items
```

- [ ] **Step 5: 跑测试，确认 5 个全 pass**

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@db:3306/geo_test \
  app pytest server/tests/test_auto_review_loop_query.py -q
```

**预期**：`5 passed`。

如果 `test_list_recent_decisions_model_label_filter` fail 提示 `JSON_EXTRACT not supported`：检查 MySQL 版本（需要 5.7+）。CI / 容器是 8.0，应该没问题。

- [ ] **Step 6: ruff + mypy 通过**

```bash
docker compose exec app ruff check server/app/modules/auto_review/service.py server/tests/test_auto_review_loop_query.py
docker compose exec app ruff format --check server/app/modules/auto_review/service.py server/tests/test_auto_review_loop_query.py
docker compose exec app mypy server/app/modules/auto_review/service.py
```

**预期**：全 0 error。如果 ruff format 报差异，去掉 `--check` 直接改写。

- [ ] **Step 7: Commit**

```bash
git add server/app/modules/auto_review/service.py server/tests/test_auto_review_loop_query.py
git commit -m "$(cat <<'EOF'
feat(auto_review): list_recent_decisions 查询函数 — 为 /goal loop 净产出验证提供 ground truth

按 decided_by + decision + 滚动时间窗 join AutoReviewDecision × Article，
可选按 Article.metrics.writer_model JSON 过滤。返回 (total_count, items[:limit])，
count 不受 limit 影响以保留真实计数。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `/today-loop-decisions` HTTP 端点（TDD）

**Files:**
- Modify: `server/app/modules/auto_review/router.py`（在 `post_auto_review` 后追加新端点）
- Append: `server/tests/test_auto_review_loop_query.py`（追加 2 个端点测试）

- [ ] **Step 1: 追加端点鉴权 + 集成测试**

在 `server/tests/test_auto_review_loop_query.py` 末尾追加：

```python
@pytest.mark.mysql
def test_today_loop_decisions_requires_mcp_token(monkeypatch):
    """无 X-MCP-Token → 401。"""
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        r = test_app.client.get("/api/articles/today-loop-decisions")
        assert r.status_code == 401
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_today_loop_decisions_returns_count_and_items(monkeypatch):
    """有 token + 命中 2 条 → count=2, items=2，结构符合契约。"""
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        a1 = _mk_article(test_app, title="x1")
        a2 = _mk_article(test_app, title="x2")
        _mk_decision(test_app, article_id=a1, score_total=82)
        _mk_decision(test_app, article_id=a2, score_total=75)

        r = test_app.client.get(
            "/api/articles/today-loop-decisions",
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["data"]["count"] == 2
        items = body["data"]["items"]
        assert len(items) == 2
        assert {it["title"] for it in items} == {"x1", "x2"}
        assert all("decided_at" in it for it in items)
        assert all(it["article_id"] in {a1, a2} for it in items)
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_today_loop_decisions_since_hours_param(monkeypatch):
    """since_hours=1 → 2 小时前的 decision 不算。"""
    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        a1 = _mk_article(test_app, title="recent")
        a2 = _mk_article(test_app, title="2h-ago")
        _mk_decision(test_app, article_id=a1)
        _mk_decision(test_app, article_id=a2, created_at=datetime.utcnow() - timedelta(hours=2))

        r = test_app.client.get(
            "/api/articles/today-loop-decisions",
            params={"since_hours": 1},
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["data"]["count"] == 1
        assert body["data"]["items"][0]["title"] == "recent"
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 跑测试，确认 3 个新用例 fail（404 Not Found）**

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@db:3306/geo_test \
  app pytest server/tests/test_auto_review_loop_query.py -q -k today_loop
```

**预期**：3 个新测试 fail。第一个是 `assert 404 == 401`（无 mount → 404，鉴权还没机会跑），第二、三个是 `assert 404 == 200`。

- [ ] **Step 3: 在 router.py 追加端点**

修改 `server/app/modules/auto_review/router.py`：

顶部 import 区追加：

```python
from fastapi import Query
```

在文件末尾（`post_auto_review` 函数之后）追加新端点：

```python
@router.get(
    "/today-loop-decisions",
    dependencies=[Depends(require_mcp_token)],
)
def get_today_loop_decisions(
    decided_by: str = "claude-goal-verifier",
    decision: str = "approved",
    since_hours: int = Query(24, ge=1, le=168),
    model_label: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """[MCP] /goal loop 的净产出验证查询。

    返回滚动时间窗内 decided_by + decision 命中的 AutoReviewDecision 行，
    join Article 拿 title，可选按 Article.metrics.writer_model 进一步过滤。

    主要消费方：`/goal` orchestrator 每轮调用一次决定是否继续循环。
    """
    from server.app.modules.auto_review.service import list_recent_decisions

    try:
        count, items = list_recent_decisions(
            db,
            decided_by=decided_by,
            decision=decision,
            since_hours=since_hours,
            model_label=model_label,
            limit=limit,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise mcp_exception_response(
            exc,
            context=f"list_today_loop_articles decided_by={decided_by} decision={decision}",
        ) from exc
    return {"ok": True, "data": {"count": count, "items": items}, "error": None}
```

注意 `list_recent_decisions` 用**函数内 import**避免顶部 import 列表变长（router 模块只有这一处用它）。

- [ ] **Step 4: 跑测试，确认 3 个新用例 pass**

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@db:3306/geo_test \
  app pytest server/tests/test_auto_review_loop_query.py -q
```

**预期**：原来 5 + 新 3 = **8 passed**。

- [ ] **Step 5: ruff + mypy 通过**

```bash
docker compose exec app ruff check server/app/modules/auto_review/router.py server/tests/test_auto_review_loop_query.py
docker compose exec app ruff format --check server/app/modules/auto_review/router.py server/tests/test_auto_review_loop_query.py
docker compose exec app mypy server/app/modules/auto_review/router.py
```

**预期**：0 error。

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/auto_review/router.py server/tests/test_auto_review_loop_query.py
git commit -m "$(cat <<'EOF'
feat(auto_review): GET /api/articles/today-loop-decisions 端点 — /goal loop 净产出查询入口

走 require_mcp_token 鉴权，参数 decided_by/decision/since_hours/model_label/limit
全部带边界 clip（since_hours 1-168, limit 1-200），异常包 mcp_exception_response
保留细节给主对话。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `list_today_loop_articles` MCP 工具注册

**Files:**
- Modify: `server/mcp/tools/catalog.py`（末尾追加一个 `@mcp.tool()` async 函数）

无自动测——MCP 工具是 `_aget` 的薄壳，行为已被 Task 2 的端点测试覆盖。本任务只验证 lint + 模块能 import。

- [ ] **Step 1: 在 catalog.py 末尾追加新工具**

修改 `server/mcp/tools/catalog.py`，文件末尾追加：

```python
@mcp.tool()
async def list_today_loop_articles(
    decided_by: str = "claude-goal-verifier",
    decision: str = "approved",
    since_hours: int = 24,
    model_label: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Count + list articles that the /goal loop wrote and verifier decided on,
    within a rolling time window.

    Used by the /goal orchestrator as the source-of-truth stop condition,
    independent of the writer subagent's self-report.

    Args:
        decided_by: AutoReviewDecision.decided_by filter. Default
            "claude-goal-verifier" matches the verifier skill convention.
        decision: AutoReviewDecision.decision filter. Default "approved".
        since_hours: Window length in hours. Default 24, cap 168 (1 week).
        model_label: Optional. If supplied, also filter
            Article.metrics.writer_model == model_label.
        limit: Max items in returned list. Default 50, cap 200.

    Returns:
        {"ok": True, "data": {"count": int, "items": [...]}, "error": None}
        on success. items: [{article_id, title, decided_at, score_total}].
    """
    params: dict[str, Any] = {
        "decided_by": decided_by,
        "decision": decision,
        "since_hours": max(1, min(168, since_hours)),
        "limit": max(1, min(200, limit)),
    }
    if model_label:
        params["model_label"] = model_label
    return await _aget("/api/articles/today-loop-decisions", params=params)
```

- [ ] **Step 2: ruff + 模块 import 自检**

```bash
docker compose exec app ruff check server/mcp/tools/catalog.py
docker compose exec app ruff format --check server/mcp/tools/catalog.py
docker compose exec app python -c "import server.mcp.tools.catalog; print('ok')"
```

**预期**：`ok`。如果 `--check` 报差异，去掉直接改写。

- [ ] **Step 3: Commit**

```bash
git add server/mcp/tools/catalog.py
git commit -m "$(cat <<'EOF'
feat(mcp): list_today_loop_articles 工具 — /goal orchestrator 净产出查询入口

catalog 组从 7 个工具增到 8 个；async + _aget 薄壳模式，端到端鉴权 + 错误
处理在后端 endpoint 已覆盖。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Skill 文件 lint 测试 + 3 个 SKILL.md frontmatter（TDD）

**Files:**
- Test: `server/tests/test_goal_skill_files.py`（新建）
- Create: `.claude/skills/geo-goal-orchestrator/SKILL.md`（stub frontmatter）
- Create: `.claude/skills/geo-article-writer/SKILL.md`（stub frontmatter）
- Create: `.claude/skills/geo-article-verifier/SKILL.md`（stub frontmatter）

- [ ] **Step 1: 写 lint 测试**

创建 `server/tests/test_goal_skill_files.py`：

```python
"""检查 .claude/skills/ 下 3 个 /goal 相关 skill 文件存在 + frontmatter 良构。

不依赖 DB / build_test_app —— 纯文件 lint。
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

_PROJECT_ROOT = pathlib.Path(__file__).parent.parent.parent


@pytest.mark.parametrize(
    "skill_relpath",
    [
        ".claude/skills/geo-goal-orchestrator/SKILL.md",
        ".claude/skills/geo-article-writer/SKILL.md",
        ".claude/skills/geo-article-verifier/SKILL.md",
    ],
)
def test_skill_file_well_formed(skill_relpath: str):
    """SKILL.md 存在 + 顶部 YAML frontmatter 含 name + description；
    description 以 'Use when' 开头（writing-skills 规范，让 Claude Code 自动触发）。"""
    path = _PROJECT_ROOT / skill_relpath
    assert path.exists(), f"missing {skill_relpath}"

    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{skill_relpath}: missing leading frontmatter delimiter"

    fm_body, sep, _ = text[4:].partition("\n---\n")
    assert sep, f"{skill_relpath}: missing closing frontmatter delimiter"

    meta = yaml.safe_load(fm_body)
    assert isinstance(meta, dict), f"{skill_relpath}: frontmatter must be a mapping"
    assert meta.get("name"), f"{skill_relpath}: name missing"
    assert meta.get("description"), f"{skill_relpath}: description missing"
    desc = meta["description"].strip()
    assert desc.lower().startswith("use when"), (
        f"{skill_relpath}: description should start with 'Use when' (got: {desc[:40]}...)"
    )
```

- [ ] **Step 2: 跑测试，确认 3 个 fail**

```bash
docker compose exec app pytest server/tests/test_goal_skill_files.py -q
```

**预期**：`3 failed`，提示 `missing .claude/skills/...`。

注意：这个测试不需要 MySQL，可以裸跑 `pytest server/tests/test_goal_skill_files.py`，但走容器统一。

- [ ] **Step 3: 创建 3 个 SKILL.md stub**

创建 `.claude/skills/geo-goal-orchestrator/SKILL.md`：

```markdown
---
name: geo-goal-orchestrator
description: Use when /goal command is invoked in geo-collab repo. Drives the
  netto-verified article generation loop with Ralph-style fresh-context writer
  + Haiku verifier subagents. Owns natural-language goal parsing, candidate
  question selection, retry/budget ceiling, and Feishu reporting.
---

TODO: filled in Task 7.
```

创建 `.claude/skills/geo-article-writer/SKILL.md`：

```markdown
---
name: geo-article-writer
description: Use when spawned as a writer subagent by /goal, or when manually
  composing one GEO article. Reads a question + template from MCP, writes
  markdown, calls save_article + (best-effort) illustrate_article, returns
  article_id.
---

TODO: filled in Task 5.
```

创建 `.claude/skills/geo-article-verifier/SKILL.md`：

```markdown
---
name: geo-article-verifier
description: Use when spawned as a verifier subagent by /goal to score a
  freshly written article. Reads article + original question + template,
  scores 4 dimensions independently, writes decision via
  submit_review_decision (does NOT change article.review_status).
---

TODO: filled in Task 6.
```

`TODO` 行是临时——下面 Tasks 5/6/7 会替换。这是**允许的临时占位**，因为后面的 task 会立刻覆盖它，而非长期遗留。

- [ ] **Step 4: 跑测试，确认 3 个 pass**

```bash
docker compose exec app pytest server/tests/test_goal_skill_files.py -q
```

**预期**：`3 passed`。

- [ ] **Step 5: Commit**

```bash
git add server/tests/test_goal_skill_files.py .claude/skills/geo-goal-orchestrator/SKILL.md .claude/skills/geo-article-writer/SKILL.md .claude/skills/geo-article-verifier/SKILL.md
git commit -m "$(cat <<'EOF'
test(.claude): skill 文件 lint + 3 个 SKILL.md frontmatter stub

测试检查文件存在 + frontmatter YAML 良构 + description 以 'Use when' 开头
（writing-skills 规范触发自动加载）。body 占位为 TODO，下游 task 即填。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 填 `geo-article-writer/SKILL.md` 完整 body

**Files:**
- Modify: `.claude/skills/geo-article-writer/SKILL.md`（保留 frontmatter，替换 TODO 占位）

- [ ] **Step 1: 替换 body**

把 `.claude/skills/geo-article-writer/SKILL.md` 的 `TODO: filled in Task 5.` 这一行整段替换为：

```markdown
# Role

你**只写一篇**文章并入库。不要循环、不要评分、不要碰其它 article。
输入由 orchestrator 在 prompt 里给你；输出按最后约定的 JSON 单行回主对话。

# Required Checklist (per spawn)

1. get question — `list_question_items(pool_id=<from input>)` 拿到 qid 对应条目；
   或直接用 input 里给的 question_text 兜底（如果 orchestrator 已经带过来）
2. get template — `list_prompt_templates(scope="generation")` 找到 tpl_id 的 content
3. 写 markdown body（约束见下）
4. `save_article(question_item_id, prompt_template_id, title, markdown_content, model_label)`
5. `illustrate_article(article_id)` — best-effort，失败吞掉
6. 返回 `{"article_id": int, "title": str}` 作为**最后一条消息**，**只输出 JSON 一行**

# title vs markdown_content 约束（重要）

- `title` 是单字段，<= 300 字符，**不要**在 `markdown_content` 顶部再写 `# 标题`
- `markdown_content` 从正文第一段开始；用 `## / ###` 做次级标题；列表 / 加粗按需
- 后端 `save_article` 会把 markdown 转 Tiptap + HTML，重复标题会进段落里污染显示

# 通用写作约束

- 内容紧扣 `question_text`
- 参考 template content 的语气 / 结构指引（template 是给你看的指令，**不是给读者看的**——不要把 template 的指令性句子写进文章）
- 不胡编事实；不可验证的数字 / 引述删除或改写
- 不触发平台合规风险（政治 / 医疗 / 灰产宣传等）

## 矩阵特例：餐厅养成记官方矩阵（默认）

- 风格：轻松实用，避免「开篇一段宏大引入」，直接进主题
- 偏好题材：游戏推荐 / 攻略 / 玩法解析 / 国风游戏综述
- 配图类别：温馨治愈、国风山水（具体 stock_category_id 让 `illustrate_article` 自动按文章 tag 选；不要写死 category_ids）

## 加新矩阵的方法（给团队同事）

1. 复制本目录为 `.claude/skills/geo-article-writer-<matrix-code>/`
2. **只改本文件「矩阵特例」这一节**；其它段落不动
3. 调用时 `/goal matrix=<matrix-code> ...`，orchestrator 会装载对应目录的 SKILL.md

# 失败处理

- `save_article` 失败（如 415 / 标题超长 / DB 冲突）
  → 输出 `{"error": "<message>"}` 退出；orchestrator 会跳过这条 qid 不再重试
- `list_question_items` / `list_prompt_templates` 失败 → 同上
- `illustrate_article` 失败 → 内吞、不上抛；文章已落库无配图也算交付

# 返回格式（**强制**）

最后一条消息只能是单行 JSON：

成功：
```
{"article_id": 824, "title": "国风游戏 2026 推荐 10 选"}
```

失败：
```
{"error": "save_article 415: unsupported markdown element"}
```

不要在 JSON 前后加任何解释 / markdown 包裹 / "我写完了" 之类的话。
orchestrator 用正则匹配最后一行 JSON 拿结果。
```

- [ ] **Step 2: 跑 skill lint 确认仍 pass**

```bash
docker compose exec app pytest server/tests/test_goal_skill_files.py -q
```

**预期**：`3 passed`（frontmatter 没动）。

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/geo-article-writer/SKILL.md
git commit -m "$(cat <<'EOF'
docs(skills): geo-article-writer SKILL.md body — 单篇写作 playbook

通用约束（title vs markdown_content / 风格基线 / 失败处理）+ 矩阵特例
（默认餐厅养成记）+ 加新矩阵的复制步骤 + 强制单行 JSON 返回格式。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: 填 `geo-article-verifier/SKILL.md` 完整 body

**Files:**
- Modify: `.claude/skills/geo-article-verifier/SKILL.md`（保留 frontmatter，替换 TODO）

- [ ] **Step 1: 替换 body**

把 `.claude/skills/geo-article-verifier/SKILL.md` 的 `TODO: filled in Task 6.` 整段替换为：

```markdown
# Role

你是**独立的**评分员。不是写文章那个 agent。你只做：按 4 个维度打分 + 出
decision + 调 `submit_review_decision`。

# Required Checklist (per spawn)

1. `get_article(article_id)` — 拿完整内容 + qid + tpl_id（从 metrics 或 input）
2. `list_question_items(pool_id=...)` 反查 qid 对应 question_text
3. `list_prompt_templates(scope="generation")` 反查 tpl_id 对应 template
4. 按 4 维度评分（0-100，整数）
5. 计算 `score_total = round((factuality + readability + style + policy_safety) / 4)`
6. 决策（门槛见下）
7. `submit_review_decision(article_id, decision, score_total, score_breakdown,
   reasoning, decided_by="claude-goal-verifier")`
8. 返回 `{"decision": str, "score_total": int}` 作为最后一条消息

# 评分维度

| 维度 | 0-100 分什么 |
|---|---|
| `factuality` | 事实正确性、有无明显胡编、数字 / 时间 / 引述是否站得住 |
| `readability` | 段落结构、连贯性、易读程度、标题层级合理性 |
| `style` | 与 template 指引的语气 / 矩阵风格的贴合度 |
| `policy_safety` | 合规风险（政治 / 医疗 / 灰产 / 违禁）—— **从严** |

# 决策门槛

- `score_total >= 70` **且** `policy_safety >= 80` → `"approved"`
- 否则 `score_total >= 40` → `"needs_rewrite"`
- 否则 → `"rejected"`

**policy_safety < 80 一律不能 approved**，即使总分高（人审兜底，但减负）。

# 反例（什么不该 approve）

- 开篇 "在这个 XX 的时代…" 这种空洞引入 → readability 扣到 60 以下
- 出现 "据某权威机构 99% 用户…" 但没有源 → factuality 扣到 60 以下
- 涉及医疗效果断言 / 投资收益承诺 → policy_safety 直接拉到 < 60
- 模板要求"轻松实用"但文章是宏大叙事 → style 扣到 60 以下

# 重要约束

- **绝不调** `set_review_status` —— 不直接动 `article.review_status`
  （保留人审兜底；项目纪律）
- `submit_review_decision` 的 `decided_by` 字段必须 = `"claude-goal-verifier"`
  （净产出验证依赖这个串筛 —— 改了会让 orchestrator 看不到你的 decision）
- 不要试图修改文章 / 重写 / 调 writer 工具——你只评分

# 返回格式（**强制**）

最后一条消息只能是单行 JSON：

```
{"decision": "approved", "score_total": 82}
```

或失败：
```
{"error": "get_article 404"}
```

不要在 JSON 前后加任何评论 / 推理过程 / "我评完了" 之类的话。
推理过程应该写入 `submit_review_decision` 的 `reasoning` 参数（1-2 句话）。
```

- [ ] **Step 2: 跑 skill lint 确认仍 pass**

```bash
docker compose exec app pytest server/tests/test_goal_skill_files.py -q
```

**预期**：`3 passed`。

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/geo-article-verifier/SKILL.md
git commit -m "$(cat <<'EOF'
docs(skills): geo-article-verifier SKILL.md body — 独立评分员 playbook

4 维度 + 决策门槛（policy_safety < 80 一律不 approve）+ 反例 + 强制
decided_by="claude-goal-verifier"（净产出查询依赖此串筛）+ 强制单行
JSON 返回。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: 填 `geo-goal-orchestrator/SKILL.md` 完整 body

**Files:**
- Modify: `.claude/skills/geo-goal-orchestrator/SKILL.md`（保留 frontmatter，替换 TODO）

- [ ] **Step 1: 替换 body**

把 `.claude/skills/geo-goal-orchestrator/SKILL.md` 的 `TODO: filled in Task 7.` 整段替换为：

```markdown
# Role

你是 `/goal` 命令的 orchestrator。在**主对话**里执行；写作 + 评分通过
`Agent` 工具下发到 fresh-context subagent。你**不写文章、不评分**——你只
做：sanity check → 解析目标 → 调度子 agent → 查 GEO 拿净产出 → 决定继续/退出
→ 飞书播报。

# Required Checklist (per /goal invocation)

1. **Sanity check** — 调 `list_question_pools()`；失败立即退出 + 提示
   "请按 docs/mcp-setup-notes.md 配 ~/.claude.json 的 mcpServers.geo"
2. **解析目标** — 从用户自由文本抽取 `{N, pool_id, topic_hint, matrix_code, model_label}`
3. **抓 candidates + templates** — `list_question_items` + `list_prompt_templates`
4. **进入主循环**（见下）
5. **退出前飞书播报** —— `notify_feishu(title, message, level)`，level ∈
   `{"done", "warning", "error"}`

# Goal Parsing 规则

从用户输入文本里抽取这些字段，缺省值如下：

| 字段 | 抽取规则 | 缺省 |
|---|---|---|
| `N` | 文中数字 + 量词（"5 篇" / "8 个" / "10 件" 都接受） | `5` |
| `pool_id` | 用户提到池名（"wenti01" / "问题池" 等） → 匹配 `list_question_pools` 里的 `name` | 第一个 `pending_count > 0` 的池 |
| `topic_hint` | 题材关键词（"国风" / "治愈" / "解谜" 等） | `None` |
| `matrix_code` | 用户写 `matrix=<code>` 才设 | `""`（用默认 geo-article-writer） |
| `model_label` | 固定 | `"claude-goal-opus-4-7"` |

# 主循环（每轮）

```pseudo
while True:
    # === 退出闸门（优先级从高到低）===
    netto = list_today_loop_articles(
        decided_by="claude-goal-verifier",
        decision="approved",
        since_hours=24,
        model_label=target.model_label,
    ).data
    echo(f"[netto] today approved by goal-verifier: {netto.count}/{target.N}")

    if netto.count >= target.N:
        notify_feishu(
            "生文 Loop 完成",
            f"净产出 {netto.count}/{target.N}, 共耗时 {minutes}m",
            "done",
        )
        return SUCCESS

    if attempts >= 3 * target.N:
        notify_feishu("生文 Loop 中止", f"attempts ceiling, 净产出 {netto.count}/{target.N}", "warning")
        return ABORT
    if len(used_qids) >= len(candidates):
        notify_feishu("生文 Loop 中止", f"候选问题用尽, 净产出 {netto.count}/{target.N}", "warning")
        return ABORT
    if estimated_main_tokens > 80_000:
        notify_feishu("生文 Loop 中止", f"token 预算触线, 净产出 {netto.count}/{target.N}", "warning")
        return ABORT
    if consecutive_mcp_fail >= 3:
        notify_feishu("生文 Loop 中止", "MCP 连续失败 3 次, 请检查后端/token", "error")
        return ABORT

    # === 选 next qid（避重）===
    qid = pick_next_qid(candidates, used_qids)
    used_qids.add(qid)
    tpl_id = templates[attempts % len(templates)].id
    attempts += 1

    # === Writer subagent（fresh context, Opus）===
    matrix_suffix = "" if target.matrix_code == "" else "-" + target.matrix_code
    writer_result = Agent(
        subagent_type="general-purpose",
        description=f"写一篇文章 qid={qid}",
        prompt=f"""Read .claude/skills/geo-article-writer{matrix_suffix}/SKILL.md and follow it strictly.

Input: qid={qid}, tpl_id={tpl_id}, model_label={target.model_label}

Output: ONLY a single-line JSON object as the final message, like:
  {{"article_id": 824, "title": "..."}}
or on failure:
  {{"error": "..."}}
No other text.""",
    )
    parsed = parse_last_json_line(writer_result.stdout)
    if "error" in parsed:
        echo(f"[round {attempts}/{3*target.N}] writer 失败: {parsed.error}")
        if is_mcp_error(parsed.error):
            consecutive_mcp_fail += 1
        continue
    consecutive_mcp_fail = 0
    article_id = parsed["article_id"]
    echo(f"[round {attempts}/{3*target.N}] writer 交稿 article_id={article_id}, verifier …")

    # === Verifier subagent（fresh context, Haiku）===
    verifier_result = Agent(
        subagent_type="general-purpose",
        model="haiku",
        description=f"评分 article_id={article_id}",
        prompt=f"""Read .claude/skills/geo-article-verifier/SKILL.md and follow it strictly.

Input: article_id={article_id}, qid={qid}, tpl_id={tpl_id}

Output: ONLY a single-line JSON object as the final message, like:
  {{"decision": "approved", "score_total": 82}}
No other text.""",
    )
    parsed_v = parse_last_json_line(verifier_result.stdout)
    if "error" in parsed_v:
        echo(f"[round {attempts}/{3*target.N}] verifier 失败, article {article_id} 留 pending 由人审")
        continue
    echo(f"[round {attempts}/{3*target.N}] verifier decision={parsed_v.decision} score={parsed_v.score_total}")
    # 不管 decision 是什么循环都继续——netto 查询会反映真实通过数
```

# 进度日志（必须 echo 这些短行）

```
[orchestrator] sanity ✓ pool=<name> N=<N> matrix=<code|default>
[round k/3N] qid=<id> → writer …
[round k/3N] writer 交稿 article_id=<id>, verifier …
[round k/3N] verifier decision=<d> score=<total>
[netto] today approved by goal-verifier: <count>/<N>
[done|abort] 净产出 <count>/<N>, 共耗时 <m>m, 原因=<...>
```

# Helper 定义（消除歧义）

| Helper | 定义 |
|---|---|
| `matrix_suffix(code)` | `code == ""` → `""`；否则 `"-" + code` |
| `topic_hint_match(item, hint)` | 不区分大小写子串匹配；`hint in item.question_text` OR `hint in item.category` |
| `pick_next_qid(candidates, used_qids)` | 按 `candidates` 顺序返第一个不在 `used_qids` 的；全用过返 None |
| `is_mcp_error(error)` | `mcp__geo__*` 返回 `{ok:false, error}` 或抛 401/502/5xx/超时 → True |
| `estimated_main_tokens` | 粗估 `attempts * 8000`；Claude Code 暴露精确 API 后再换 |
| `parse_last_json_line(text)` | 找最后一行能 `json.loads` 解析的；找不到返 `{"error": "no JSON in subagent output"}` |

# Stop / Budget Rules（再次强调）

- `netto.count >= N` → SUCCESS（飞书 done）
- `attempts >= 3N` → ABORT（飞书 warning）
- candidates 用尽 → ABORT（飞书 warning）
- 估算主对话 token > 80k → ABORT（飞书 warning）
- 连续 MCP 错误 >= 3 → ABORT（飞书 error）
- 用户 Ctrl-C → 主对话 echo `[interrupted] 已落库 X 篇, 净产出 Y/N, 下次 /goal 会接力`（不发飞书）

# 三个不变式（硬约束）

1. **单点失败不杀 loop**——除非 MCP 连续 3 次
2. **落库失败 ≠ 验证失败**：save_article 失败 → qid 加入 used_qids 不重试；verifier 失败 → 文章留 pending 由人审
3. **netto 是唯一计数事实**：subagent 自报"我写好了"都不算数，必须查 MCP
```

- [ ] **Step 2: 跑 skill lint 确认仍 pass**

```bash
docker compose exec app pytest server/tests/test_goal_skill_files.py -q
```

**预期**：`3 passed`。

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/geo-goal-orchestrator/SKILL.md
git commit -m "$(cat <<'EOF'
docs(skills): geo-goal-orchestrator SKILL.md body — /goal 主对话调度 playbook

自然语言目标解析 + Ralph 风格主循环（净产出验证 → ceiling → writer →
verifier）+ helper 定义 + 强制日志格式 + 三个不变式（单点失败不杀 loop /
落库失败≠验证失败 / netto 是唯一计数事实）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: `/goal` slash command 文件

**Files:**
- Create: `.claude/commands/goal.md`

slash command 文件本身很薄——只做"加载 orchestrator skill + 把用户输入透传给它"。

- [ ] **Step 1: 创建 command 文件**

创建 `.claude/commands/goal.md`：

```markdown
---
description: Geo 协作平台生文 Loop 入口。自然语言目标 → Ralph 风格自动产出 N 篇过自评文章入未审核库 → 飞书播报。
---

# /goal — Geo 生文 Loop

你刚被 `/goal $ARGUMENTS` 调用。把这条命令当作 `geo-goal-orchestrator`
skill 的入口包装：

1. **立刻** invoke the `geo-goal-orchestrator` skill（用 Skill tool）来装载完整 playbook。
2. 装载后，按 skill 里的 Required Checklist 一项一项执行，把 `$ARGUMENTS` 当作用户的自由文本目标传给「Goal Parsing 规则」段。
3. **不要**在装载 skill 之前先自己解析目标或调 MCP；skill 内部第一步就是 sanity check，让它来跑。

## 同事第一次用 /goal 之前要看的

如果是你（同事）第一次在本仓库里跑 `/goal`，先打开 `.claude/README.md`
完成 5 步 onboarding（MCP token 配置等）；不然 sanity check 会立刻失败。

## 这条命令做什么 / 不做什么

**做**：
- 自然语言目标解析（"今天 5 篇国风游戏文章"）
- 自动选题（从问题池避重）
- 启动多个 fresh-context subagent 分别写文章 + 评分
- 把净产出查 GEO 拿 ground truth 作停止条件
- 完成后飞书群播报

**不做**：
- 不发布（分发走独立 loop）
- 不直接改 `article.review_status`（人审兜底）
- 不在主对话里写文章草稿（子 agent 干，不污染主 context）
```

- [ ] **Step 2: 验证文件 + 手动测试装载**

```bash
ls -la .claude/commands/goal.md
```

**手动验证**（不进自动测）：

1. 重启 Claude Code
2. 在 Claude Code 输入 `/goal --help`（或直接 `/goal`），确认主对话开始装载 `geo-goal-orchestrator` skill
3. 不需要完整跑 loop——能装载成功即可

如果装载失败，常见原因：
- 文件路径不对 → 确认 `.claude/commands/goal.md` 在项目根
- skill 名拼错 → 跟 Task 7 的 `name:` 字段一致

- [ ] **Step 3: Commit**

```bash
git add .claude/commands/goal.md
git commit -m "$(cat <<'EOF'
feat(.claude): /goal slash command — Geo 生文 Loop 入口

薄壳设计：command 只负责装载 geo-goal-orchestrator skill 并把 $ARGUMENTS
透传给它的「Goal Parsing 规则」段。具体调度逻辑全部在 skill 里。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: `.claude/README.md` onboarding 文档

**Files:**
- Create: `.claude/README.md`

- [ ] **Step 1: 创建 README**

创建 `.claude/README.md`：

```markdown
# `.claude/` — Geo 协作平台 Claude Code 工程目录

本目录里的东西**跟 git 走**，同事 `git pull` 后第一次在仓库里启动
Claude Code 即可使用。无需单独安装。

## 文件清单

| 文件 | 干什么 |
|---|---|
| `commands/goal.md` | `/goal` slash command — Geo 生文 Loop 入口 |
| `skills/geo-goal-orchestrator/` | 主对话调度 playbook（被 /goal 装载） |
| `skills/geo-article-writer/` | writer subagent playbook（每篇文章一个 fresh subagent） |
| `skills/geo-article-verifier/` | verifier subagent playbook（每篇文章一个 Haiku subagent 评分） |

设计原始稿：`docs/superpowers/specs/2026-06-24-goal-loop-engineering-design.md`。

---

## 第一次用 /goal —— 5 步 onboarding

```
1. git pull                              # 拿最新 .claude/ 目录
2. 一次性配置（每台机器一次）
   - 打开 ~/.claude.json，加 mcpServers.geo 段
   - 把后端管理员发的 GEO_MCP_TOKEN 填到 headers.X-MCP-Token
   - 详细参考 docs/mcp-setup-notes.md
3. 重启 Claude Code
4. 在 Claude Code 里输入 /mcp，确认 geo server 显示 "connected"
5. 在 Claude Code 里输入：
   /goal 帮我今天产出 5 篇关于国风游戏的文章

之后 /goal 会自动跑（约 10-20 分钟）；完成后飞书群会有播报。
```

---

## 跑 /goal 时主对话会出现什么

干净的状态条，不会被子 agent 写作 / 评分细节污染：

```
[orchestrator] sanity ✓ pool=问题池 N=5 matrix=default
[round 1/15] qid=123 → writer …
[round 1/15] writer 交稿 article_id=824, verifier …
[round 1/15] verifier decision=approved score=82
[netto] today approved by goal-verifier: 1/5
[round 2/15] qid=124 → writer …
...
[done] 净产出 5/5, 共耗时 12m, 飞书已播报
```

---

## 复用 / 定制路径

| 想改的事 | 怎么改 |
|---|---|
| 默认 N | 直接说：`/goal 今天 8 篇` |
| 默认问题池 | 直接说：`/goal 用 wenti01 池产出 5 篇` |
| 加新内容矩阵 | 复制 `skills/geo-article-writer/` 为 `geo-article-writer-<code>/`，**只改 `## 矩阵特例` 段**；调用 `/goal matrix=<code> ...` |
| 单独写一篇（不走 loop） | 主对话 `Skill geo-article-writer` 进入写作模式手动配合写——**不评分、不计 netto** |
| 改评分门槛 | 改 `skills/geo-article-verifier/SKILL.md` 的「决策门槛」段 |

---

## 常见排障

| 症状 | 原因 | 处理 |
|---|---|---|
| `/goal` 启动后立刻退出，提示 "MCP 不可用" | `~/.claude.json` 没配 / token 错 | 走上面 onboarding 第 2 步 |
| 跑到一半 attempts 用完但 netto=0 | verifier 一直不给 approved（评分门槛太严 / 选题质量差） | 单独 `Skill geo-article-writer` 试一题看写作质量；写作没问题就是 verifier 门槛 |
| writer 报 `save_article 415` | markdown 里塞了不支持的元素（罕见） | 给后端工程师看错误 detail |
| 配图全部失败 | stock_category 没配 / category_id 不对 | 不致命，文章已落库；联系平台扩展同事配 |
| 飞书没收到播报 | webhook 没配 / 配错环境 | 检查后端 `GEO_FEISHU_WEBHOOK_URL` |

---

## 三类同事的接触面

| 角色 | 想做什么 | 看哪几个文件 |
|---|---|---|
| **运营**（90%） | 跑 `/goal` 出文章 | 本 README 就够 |
| **写作风格调优** | 改矩阵风格 / 加新矩阵 | `skills/geo-article-writer/SKILL.md` 的「矩阵特例」段 |
| **平台扩展** | 加新 stop 条件 / 评分维度 / MCP 工具 | orchestrator skill + 后端 `auto_review/service.py` + `mcp/tools/catalog.py` |
```

- [ ] **Step 2: Commit**

```bash
git add .claude/README.md
git commit -m "$(cat <<'EOF'
docs(.claude): README 同事 onboarding — 5 步配置 + 排障表 + 复用路径

让任何同事 git pull 之后第一时间知道 .claude/ 目录是什么、/goal 怎么用、
出错时去哪查。也是 spec §4.2-§4.5 的执行落地。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: 后端 lint / typecheck / 全测 + 手工冒烟

不引入新文件；本任务是**集成验证**，确保 Tasks 1-9 加起来在 CI 门禁下能过、在真实 Claude Code 里能跑。

- [ ] **Step 1: 后端硬门禁**

```bash
docker compose exec app ruff check server/
docker compose exec app ruff format --check server/
docker compose exec app mypy server/app
```

**预期**：0 error。如果 ruff format 报差异，去掉 `--check` 直接改写、再 commit 一次 `style: ruff format`。

- [ ] **Step 2: 后端全部测试**

```bash
docker compose exec -e GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@db:3306/geo_test \
  app pytest server/tests/ -q
```

**预期**：全 pass（新增 8 + 3 = 11 用例，不影响存量）。

- [ ] **Step 3: 手工冒烟（spec §8.2）**

在本机 Claude Code 里跑：

```
/goal 帮我产出 1 篇国风游戏文章作为冒烟
```

按以下检查表逐项验证：

| # | 步骤 | 期望 |
|---|---|---|
| 1 | 主对话 echo `[orchestrator] sanity ✓ pool=...` | ✓ |
| 2 | 主对话 echo `[round 1/3] qid=... → writer …` | writer subagent 启动 |
| 3 | 主对话 echo `[round 1/3] writer 交稿 article_id=...` | `save_article` 成功 |
| 4 | 主对话 echo `[round 1/3] verifier decision=... score=...` | verifier subagent 启动并完成 |
| 5 | 主对话 echo `[netto] today approved by goal-verifier: 1/1` | MCP 查询正确 |
| 6 | 主对话 echo `[done] 净产出 1/1, 共耗时 X m` | 退出路径正确 |
| 7 | 飞书群里有 done 级播报 | webhook 链路通 |
| 8 | GEO web UI 文章列表能看到这条 article | 数据库可见 |
| 9 | article 的 `review_status="pending"`（**没被自动 approved**） | 人审兜底纪律保住 |
| 10 | `auto_review_decisions` 表里有对应行，`decided_by="claude-goal-verifier"` | 净产出查询依赖项保住 |

冒烟中遇到的问题修复方式：

- **第 1 步就 MCP 不通**：检查本机 `~/.claude.json` 的 mcpServers.geo + token；本任务不改后端，只确保 onboarding 文档清楚（已经在 Task 9 写了）
- **第 2-3 步 writer 不出活**：先 `Skill geo-article-writer` 手动配合写一题，看 skill 内容是否清楚到能让 fresh-context subagent 独立完成；如不清楚补 skill body
- **第 4 步 verifier 失败**：同上方法测 verifier skill
- **第 5 步 netto 数对不上**：检查 `model_label` 一致性——orchestrator 传 `claude-goal-opus-4-7`，writer 传同一串，verifier `decided_by=claude-goal-verifier`，查询用这俩串筛
- **第 7 步飞书没收到**：检查后端 `GEO_FEISHU_WEBHOOK_URL`
- **第 9 步 review_status 变成 approved**：bug——verifier skill 错调了 `set_review_status`，回 Task 6 重申「绝不调 set_review_status」

- [ ] **Step 4: 把冒烟结果记到一个 commit（可选）**

如果冒烟全过，可加一个空 commit 标记里程碑：

```bash
git commit --allow-empty -m "$(cat <<'EOF'
chore(goal-loop): 手工冒烟 10 步全通过 — 可发起 PR

后端 ruff/format/mypy/pytest 全绿；/goal 端到端跑 1 篇 → article 落库 +
review_status=pending + auto_review_decisions 命中 + 飞书 done 播报。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

如果发现需要补：

- 补完后单独 commit `fix(...)`，不要 amend 已有的 task commit
- 重跑相关 Step 直至全过

- [ ] **Step 5: 推分支 + 发 PR**

```bash
git push -u origin docs/goal-loop-engineering
gh pr create --title "feat(goal-loop): /goal slash command + Ralph 风格 subagent + 净产出验证" --body "$(cat <<'EOF'
## Summary

- 把生文 Loop 从「主对话顺序写 N 篇 + writer 自评」升级到「/goal 编排 + 独立 fresh-context writer/verifier subagents + 净产出验证作停止条件」
- 新增 1 个 MCP 工具 `list_today_loop_articles` + 1 个后端端点 `GET /api/articles/today-loop-decisions`
- 新增 3 个项目级 SKILL.md（orchestrator / writer / verifier）+ 1 个 slash command + `.claude/README.md` onboarding

设计稿：`docs/superpowers/specs/2026-06-24-goal-loop-engineering-design.md`
实施计划：`docs/superpowers/plans/2026-06-24-goal-loop-engineering.md`

## Test plan

- [x] 后端 ruff / format / mypy / pytest 全过（CI 门禁）
- [x] 11 个新 unit 测试通过（service 5 + endpoint 3 + skill lint 3）
- [x] 手工冒烟 10 步全通过（spec §8.2）
- [ ] 至少 1 个非作者同事按 `.claude/README.md` 流程独立跑通 `/goal`（上线门禁）

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**1. Spec coverage check** — 每节是否有对应 task？

| Spec 节 | Task 覆盖 |
|---|---|
| §0-§1 决策快照 | 总览部分引用 |
| §2 架构总览 / 文件布局 | 整体 task 顺序匹配 |
| §3.1 输入 | Task 8 `/goal $ARGUMENTS` 透传 |
| §3.2 主循环伪码 | Task 7 SKILL.md 「主循环」段 |
| §3.2.1 Helper 定义 | Task 7 SKILL.md 「Helper 定义」段 |
| §3.3 进度日志 | Task 7 SKILL.md 「进度日志」段 |
| §3.4 退出 → 飞书 level | Task 7 SKILL.md 「Stop / Budget Rules」段 |
| §4.1-§4.6 同事使用 + 复用 | Task 9 `.claude/README.md` |
| §5.1 orchestrator skill | Task 7 |
| §5.2 writer skill | Task 5 |
| §5.3 verifier skill | Task 6 |
| §5.4 共同设计原则 | 隐含落实在 Tasks 5/6/7 的写法 |
| §6 MCP 工具 | Tasks 1 (service) + 2 (router) + 3 (tool) |
| §7 失败矩阵 + 不变式 | Task 7 SKILL.md 「Stop / Budget Rules」+「三个不变式」段 |
| §8.1 自动测 | Task 1 (5 用例) + Task 2 (3 用例) + Task 4 (3 用例) = 11 |
| §8.2 手工冒烟 10 步 | Task 10 Step 3 |
| §8.3 不测的事 | 隐含落实（不出现在 task 列表里） |
| §9 工作量估算 + 顺序 | task 顺序匹配 §9.2（后端 → skill → command → 冒烟） |
| §10 与已有 spec / 实现的关系 | 在本 plan 的 Architecture 段引用 |
| §11 上线门禁 | Task 10 Step 5 PR 描述里 Test plan checklist |
| §12 Out of Scope | 不需要 task |

**结论：全覆盖**。

**2. Placeholder scan** — `TODO` 出现在 Task 4 Step 3 的 SKILL.md stub 里，但 Tasks 5/6/7 立刻覆盖之；这是**有意临时占位**而不是遗留 placeholder。其它无 TBD / "fill in later" / "similar to Task N"。✓

**3. Type consistency**
- `list_recent_decisions(...)` 在 Task 1 Step 4 实现签名、Task 2 Step 3 router 内调用、Task 3 Step 1 MCP wrapper 间接调用——签名一致：`(db, *, decided_by, decision, since_hours, model_label=None, limit=50) -> tuple[int, list[dict]]`
- `decided_by="claude-goal-verifier"` 在 Tasks 1/2/3/6/7 中保持一致（verifier skill 写、netto 查询 default、orchestrator 调用）
- `model_label="claude-goal-opus-4-7"` 在 Tasks 7 (orchestrator) 与 spec §3.2 一致
- 端点路径 `/api/articles/today-loop-decisions` 在 Tasks 2/3 + spec §6.4 一致

**结论：一致**。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-24-goal-loop-engineering.md`. Two execution options:

**1. Subagent-Driven (recommended)** — 我每个 task 起一个 fresh subagent 跑，task 之间我 review，迭代快。

**2. Inline Execution** — 我在当前会话里逐 task 执行，每 2-3 个 task 检查一次。

Which approach?
