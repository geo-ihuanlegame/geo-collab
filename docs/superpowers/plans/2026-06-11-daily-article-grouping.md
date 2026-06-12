# 按日期归组 + 审核显示丝滑化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** AI 生文产出的新文章可按天累加进同一个日期分组，且未审核/已审核混合时在两个标签都可见、approve 不跳组。

**Architecture:** 后端给 `to_review` 节点加 `daily_group` 布尔开关；新 service 函数 `mark_pending_and_append_daily` 按 `(user_id, "每日生成 · 日期")` 查找-或-新建分组并去重追加。前端 `ContentWorkspace` 改标签归属：混合组双标签可见、各列本侧文章 + 跨标签提示。

**Tech Stack:** FastAPI + SQLAlchemy（MySQL）、pytest（`@pytest.mark.mysql`，`GEO_TEST_DATABASE_URL` 已设）、React 19 + TS（无单测框架，门禁 = typecheck + build）。

**依赖：** T1 → T2（节点依赖 service）。T3（前端）与后端并行。

**测试环境（本机）：** conda activate 在工具 shell 不生效；用 conda 环境的 python 全路径跑 pytest。先确认：`conda env list` 找到 `geo_xzpt` 的 python，例如 `C:\Users\Administrator\miniconda3\envs\geo_xzpt\python.exe -m pytest ...`。`GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:GeoUser20260513A1@127.0.0.1:3306/geo_test` 已在环境变量里。

---

## Task 1：后端 service — `mark_pending_and_append_daily`

**Files:**
- Modify: `server/app/modules/articles/service.py`（在 `mark_pending_and_group` 之后新增函数）
- Test: `server/tests/test_daily_grouping.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `server/tests/test_daily_grouping.py`：

```python
import pytest

from server.tests.utils import build_test_app


def _make_article(client, title="文章"):
    r = client.post(
        "/api/articles",
        json={
            "title": title,
            "content_json": {"type": "doc", "content": []},
            "content_html": "<p>x</p>",
            "plain_text": "x",
            "word_count": 1,
            "status": "ready",
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


@pytest.mark.mysql
def test_append_daily_accumulates_and_dedups(monkeypatch):
    from server.app.modules.articles.models import Article, ArticleGroup, ArticleGroupItem
    from server.app.modules.articles.service import mark_pending_and_append_daily

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a1, a2, a3 = (_make_article(client, t) for t in ("甲", "乙", "丙"))
        with app.session_factory() as db:
            uid = db.get(Article, a1).user_id

        name = "每日生成 · 2026-06-11"
        gid1 = mark_pending_and_append_daily(
            app.session_factory, article_ids=[a1, a2], user_id=uid, group_name=name
        )
        # 第二次：含重复的 a2 + 新的 a3 → 复用同组、去重
        gid2 = mark_pending_and_append_daily(
            app.session_factory, article_ids=[a2, a3], user_id=uid, group_name=name
        )

        assert gid1 is not None and gid1 == gid2  # 同一个日期分组
        with app.session_factory() as db:
            groups = (
                db.query(ArticleGroup)
                .filter(ArticleGroup.user_id == uid, ArticleGroup.is_deleted == False)  # noqa: E712
                .all()
            )
            assert len(groups) == 1  # 只一个组
            items = db.query(ArticleGroupItem).filter(ArticleGroupItem.group_id == gid1).all()
            assert {it.article_id for it in items} == {a1, a2, a3}  # 三篇、不重复
            assert len(items) == 3
            for aid in (a1, a2, a3):
                assert db.get(Article, aid).review_status == "pending"  # 全部置 pending
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_append_daily_different_name_makes_new_group(monkeypatch):
    from server.app.modules.articles.models import Article, ArticleGroup
    from server.app.modules.articles.service import mark_pending_and_append_daily

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a1, a2 = (_make_article(client, t) for t in ("甲", "乙"))
        with app.session_factory() as db:
            uid = db.get(Article, a1).user_id
        g1 = mark_pending_and_append_daily(
            app.session_factory, article_ids=[a1], user_id=uid, group_name="每日生成 · 2026-06-11"
        )
        g2 = mark_pending_and_append_daily(
            app.session_factory, article_ids=[a2], user_id=uid, group_name="每日生成 · 2026-06-12"
        )
        assert g1 != g2  # 跨天 → 两个组
        with app.session_factory() as db:
            cnt = (
                db.query(ArticleGroup)
                .filter(ArticleGroup.user_id == uid, ArticleGroup.is_deleted == False)  # noqa: E712
                .count()
            )
            assert cnt == 2
    finally:
        app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `<python> -m pytest server/tests/test_daily_grouping.py -q`
Expected: FAIL — `ImportError: cannot import name 'mark_pending_and_append_daily'`

- [ ] **Step 3: 实现函数**

在 `server/app/modules/articles/service.py` 的 `mark_pending_and_group` 函数之后新增（确认文件顶部已有 `from sqlalchemy import func, select`、`ArticleGroup`、`ArticleGroupItem`、`Article`、`utcnow`、`_logger`；`func` 已被 `compute_group_review_summary` 使用，存在）：

```python
def mark_pending_and_append_daily(
    session_factory,
    *,
    article_ids: list[int],
    user_id: int,
    group_name: str,
) -> int | None:
    """把文章标 review_status='pending' 并追加进 (user_id, group_name) 分组：
    有同名未软删组则复用，软删同名组则复活，都没有则新建；去重追加，sort_order 接 max+1。
    并发两个 run 同时建组撞 (user_id, name) 唯一约束 → rollback、重标 pending、回查复用。
    尽力而为：失败记日志、不抛；独立 session、本函数内 commit+close。返回 group_id 或 None。"""
    if not article_ids:
        return None
    try:
        from sqlalchemy.exc import IntegrityError

        db = session_factory()
        try:

            def _mark_pending() -> None:
                for aid in article_ids:
                    art = db.get(Article, aid)
                    if art is not None:
                        art.review_status = "pending"

            def _resolve_group() -> ArticleGroup:
                existing = (
                    db.query(ArticleGroup)
                    .filter(ArticleGroup.user_id == user_id, ArticleGroup.name == group_name)
                    .first()
                )
                if existing is not None:
                    if existing.is_deleted:  # 软删同名 → 复活并清空旧成员
                        existing.is_deleted = False
                        existing.deleted_at = None
                        existing.version += 1
                        existing.updated_at = utcnow()
                        existing.items.clear()
                        db.flush()
                    return existing
                grp = ArticleGroup(user_id=user_id, name=group_name)
                db.add(grp)
                db.flush()  # 撞唯一约束在此抛 IntegrityError
                return grp

            _mark_pending()
            try:
                group = _resolve_group()
            except IntegrityError:
                db.rollback()
                _mark_pending()
                group = (
                    db.query(ArticleGroup)
                    .filter(
                        ArticleGroup.user_id == user_id,
                        ArticleGroup.name == group_name,
                        ArticleGroup.is_deleted.is_(False),
                    )
                    .first()
                )
                if group is None:
                    raise

            existing_ids = {
                row[0]
                for row in db.query(ArticleGroupItem.article_id)
                .filter(ArticleGroupItem.group_id == group.id)
                .all()
            }
            max_order = (
                db.query(func.max(ArticleGroupItem.sort_order))
                .filter(ArticleGroupItem.group_id == group.id)
                .scalar()
            )
            next_order = (max_order + 1) if max_order is not None else 0
            for aid in article_ids:
                if aid in existing_ids:
                    continue
                db.add(ArticleGroupItem(group_id=group.id, article_id=aid, sort_order=next_order))
                existing_ids.add(aid)
                next_order += 1

            group.updated_at = utcnow()
            gid = group.id
            db.commit()
            return gid
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — 尽力而为
        _logger.exception(
            "mark_pending_and_append_daily failed (user=%s, name=%s, n=%s)",
            user_id,
            group_name,
            len(article_ids),
        )
        return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `<python> -m pytest server/tests/test_daily_grouping.py -q`
Expected: PASS（2 passed）

- [ ] **Step 5: lint + 提交**

```bash
ruff check server/app/modules/articles/service.py server/tests/test_daily_grouping.py
ruff format server/app/modules/articles/service.py server/tests/test_daily_grouping.py
git add server/app/modules/articles/service.py server/tests/test_daily_grouping.py
git commit -m "feat(articles): mark_pending_and_append_daily 按日期分组去重追加"
```

---

## Task 2：后端节点 — `to_review` 的 `daily_group` 分支 + config_schema

**依赖：** Task 1 已完成（用到 `mark_pending_and_append_daily`）。

**Files:**
- Modify: `server/app/modules/pipelines/nodes/to_review.py`
- Modify: `server/app/modules/pipelines/router.py:142-147`（to_review 的 config_schema）
- Modify: `server/tests/test_daily_grouping.py`（追加节点级 + node-types 测试）
- Modify: `CLAUDE.md`（pipelines 模块段落补一句 daily_group）

- [ ] **Step 1: 写失败测试**（追加到 `server/tests/test_daily_grouping.py` 末尾）

```python
@pytest.mark.mysql
def test_to_review_daily_group_accumulates(monkeypatch):
    from server.app.modules.articles.models import Article, ArticleGroup, ArticleGroupItem
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.to_review import run_to_review

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a1, a2, a3 = (_make_article(client, t) for t in ("甲", "乙", "丙"))
        with app.session_factory() as db:
            uid = db.get(Article, a1).user_id

        def _ctx(ids):
            return NodeRunContext(
                session_factory=app.session_factory,
                user_id=uid,
                config={"daily_group": True},
                inputs={"article_ids": ids},
                upstream={},
            )

        r1 = run_to_review(_ctx([a1, a2]))
        r2 = run_to_review(_ctx([a3]))  # 同一天第二次运行
        assert r1.output["group_id"] == r2.output["group_id"]  # 累加进同组
        with app.session_factory() as db:
            groups = (
                db.query(ArticleGroup)
                .filter(ArticleGroup.user_id == uid, ArticleGroup.is_deleted == False)  # noqa: E712
                .all()
            )
            assert len(groups) == 1
            assert groups[0].name.startswith("每日生成 · ")
            items = db.query(ArticleGroupItem).filter(
                ArticleGroupItem.group_id == groups[0].id
            ).all()
            assert {it.article_id for it in items} == {a1, a2, a3}
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_to_review_default_makes_new_group_each_run(monkeypatch):
    from server.app.modules.articles.models import Article, ArticleGroup
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.to_review import run_to_review

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a1, a2 = (_make_article(client, t) for t in ("甲", "乙"))
        with app.session_factory() as db:
            uid = db.get(Article, a1).user_id

        def _ctx(ids):
            return NodeRunContext(
                session_factory=app.session_factory, user_id=uid,
                config={}, inputs={"article_ids": ids}, upstream={},
            )

        r1 = run_to_review(_ctx([a1]))
        r2 = run_to_review(_ctx([a2]))
        assert r1.output["group_id"] != r2.output["group_id"]  # 默认每次新组（现状不变）
        with app.session_factory() as db:
            cnt = (
                db.query(ArticleGroup)
                .filter(ArticleGroup.user_id == uid, ArticleGroup.is_deleted == False)  # noqa: E712
                .count()
            )
            assert cnt == 2
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_node_types_to_review_has_daily_group_toggle(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        r = app.client.get("/api/pipelines/node-types")
        assert r.status_code == 200, r.text
        types = {nt["type"]: nt for nt in r.json()["node_types"]}
        fields = {f["key"]: f for f in types["to_review"]["config_schema"]}
        assert "daily_group" in fields
        assert fields["daily_group"]["type"] == "toggle"
    finally:
        app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `<python> -m pytest server/tests/test_daily_grouping.py -q`
Expected: 新增 3 个 FAIL（`daily_group` 未实现：默认/同名行为不符 + node-types 无该字段）

- [ ] **Step 3: 改 `to_review.py`**

整体替换 `server/app/modules/pipelines/nodes/to_review.py` 为：

```python
"""to_review 动作节点（「进入未审核库」）：把上游文章置 pending 并成一个组，输出 group_id。

输出 group_id 即向执行器表明「已成组」，执行器不再兜底成组（见 executor 的成组逻辑）。
daily_group=True 时按天归组：当天所有运行/流水线并入同一个「每日生成 · 日期」分组。"""

import datetime as dt
from zoneinfo import ZoneInfo

from server.app.core.config import get_settings
from server.app.modules.articles.service import (
    mark_pending_and_append_daily,
    mark_pending_and_group,
)
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register


def run_to_review(ctx: NodeRunContext) -> NodeResult:
    cfg = ctx.config or {}
    article_ids = ctx.inputs.get("article_ids") or cfg.get("article_ids") or []
    if not article_ids:
        return NodeResult(output={"skipped": "无文章"}, article_ids=[])

    if cfg.get("daily_group"):
        today = dt.datetime.now(ZoneInfo(get_settings().scheduler_tz)).date()
        gid = mark_pending_and_append_daily(
            ctx.session_factory,
            article_ids=list(article_ids),
            user_id=ctx.user_id,
            group_name=f"每日生成 · {today:%Y-%m-%d}",
        )
    else:
        base_name = (cfg.get("group_name") or "").strip() or "未审核 · 智能体生成"
        gid = mark_pending_and_group(
            ctx.session_factory,
            article_ids=list(article_ids),
            user_id=ctx.user_id,
            base_name=base_name,
            fallback_suffix=f"#{article_ids[0]}",
        )
    return NodeResult(output={"group_id": gid, "article_ids": list(article_ids)}, article_ids=[])


register("to_review", run_to_review)
```

> 确认 `get_settings` 路径：grep `from server.app.core.config import get_settings`（scheduler.py 同款导入）。若实际为 `server.app.core.config` 之外的路径，按 grep 结果改。

- [ ] **Step 4: 改 `router.py` config_schema**

`server/app/modules/pipelines/router.py` 的 to_review 段（约 142-147 行）改为：

```python
            {
                "type": "to_review",
                "label": "进入未审核库",
                "config_schema": [
                    {"key": "group_name", "type": "text", "label": "分组名(可空)"},
                    {
                        "key": "daily_group",
                        "type": "toggle",
                        "label": "按天归组",
                        "hint": "开启后，当天所有运行/流水线产出并入同一个「每日生成 · 日期」分组",
                        "default": False,
                    },
                ],
            },
```

- [ ] **Step 5: 跑测试确认通过**

Run: `<python> -m pytest server/tests/test_daily_grouping.py -q`
Expected: PASS（5 passed）

- [ ] **Step 6: 补 CLAUDE.md**

在 `CLAUDE.md` 的 pipelines 模块段落里 `to_review`（送审）描述处补一句：「`to_review` 支持 `daily_group` 开关：开启后按 `GEO_SCHEDULER_TZ` 当天日期归入同一个「每日生成 · 日期」分组（去重追加），关闭=每次运行新建组（默认）」。

- [ ] **Step 7: lint + 提交**

```bash
ruff check server/app/modules/pipelines/nodes/to_review.py server/app/modules/pipelines/router.py
ruff format server/app/modules/pipelines/nodes/to_review.py server/app/modules/pipelines/router.py
mypy server/app
git add server/app/modules/pipelines/nodes/to_review.py server/app/modules/pipelines/router.py server/tests/test_daily_grouping.py CLAUDE.md
git commit -m "feat(pipelines): to_review 新增按天归组开关 daily_group"
```

---

## Task 3：前端内容页 — 混合组双标签可见 + 跨标签提示（与 T1/T2 并行）

**Files:**
- Modify: `web/src/features/content/ContentWorkspace.tsx`
- Modify: `web/src/styles.css`（加 `.groupCrossTabHint` 样式）

**无单测框架** → 步骤为：改代码 → typecheck → build → 手动核对。

- [ ] **Step 1: 替换 `groupReviewTab` 为 `groupHasStatus`**

`ContentWorkspace.tsx` 约 437-442 行，把：

```tsx
  // A group's tab is derived from its members: fully approved → "approved", otherwise "pending".
  // Groups have no own review_status field; empty groups (total === 0) count as pending.
  function groupReviewTab(group: ArticleGroup): ReviewStatus {
    const counts = groupReviewCounts(group);
    return counts.total > 0 && counts.approved === counts.total ? "approved" : "pending";
  }
```

替换为：

```tsx
  // 组在某标签是否可见：有该状态成员就出现（混合组两个标签都在）。空组(total=0)算 pending。
  function groupHasStatus(group: ArticleGroup, status: ReviewStatus): boolean {
    const counts = groupReviewCounts(group);
    if (counts.total === 0) return status === "pending";
    const pendingCount = counts.total - counts.approved;
    return status === "approved" ? counts.approved > 0 : pendingCount > 0;
  }
```

- [ ] **Step 2: `reviewCounts` 混合组两侧各计 1**

约 453-456 行，把：

```tsx
    for (const group of groups) {
      if (groupReviewTab(group) === "approved") approved += 1;
      else pending += 1;
    }
```

替换为：

```tsx
    for (const group of groups) {
      if (groupHasStatus(group, "pending")) pending += 1;
      if (groupHasStatus(group, "approved")) approved += 1;
    }
```

- [ ] **Step 3: `unifiedList` 按当前标签可见性收组**

约 467-473 行，把：

```tsx
    // Groups land in the tab matching their derived approval state (no longer both tabs).
    for (const group of groups) {
      if (groupReviewTab(group) !== reviewTab) continue;
      if (!query || group.name.toLowerCase().includes(query.toLowerCase()) || group.items.some((item) => articleById[item.article_id])) {
        items.push({ type: "group", group, sortTime: new Date(group.created_at).getTime() });
      }
    }
```

替换为：

```tsx
    // 混合组双标签可见：当前标签有对应状态成员就纳入。
    for (const group of groups) {
      if (!groupHasStatus(group, reviewTab)) continue;
      if (!query || group.name.toLowerCase().includes(query.toLowerCase()) || group.items.some((item) => articleById[item.article_id])) {
        items.push({ type: "group", group, sortTime: new Date(group.created_at).getTime() });
      }
    }
```

- [ ] **Step 4: 组内只列本侧文章 + 组头动作按标签 + 跨标签提示**

约 1082-1136 行的组渲染。把：

```tsx
              const { group } = item;
              const isExpanded = expandedGroupIds.has(group.id);
              const groupArticles = groupArticleSummaries(group);
              const counts = groupReviewCounts(group);
              const fullyApproved = counts.total > 0 && counts.approved === counts.total;
```

替换为：

```tsx
              const { group } = item;
              const isExpanded = expandedGroupIds.has(group.id);
              const counts = groupReviewCounts(group);
              const fullyApproved = counts.total > 0 && counts.approved === counts.total;
              // 只列当前标签状态的文章；另一侧成员数用于跨标签提示。
              const groupArticles = groupArticleSummaries(group).filter(
                (a) => a.review_status === reviewTab,
              );
              const otherCount =
                reviewTab === "pending" ? counts.approved : counts.total - counts.approved;
```

然后把组头动作按钮块（约 1112-1130 行）：

```tsx
                      {fullyApproved ? (
                        <button
                          type="button"
                          className="inlineMiniButton distributeMiniButton"
                          onClick={() => openDistributeForGroup(group)}
                        >
                          <Send size={13} />
                          自动分发
                        </button>
                      ) : counts.total > 0 ? (
                        <button
                          type="button"
                          className="inlineMiniButton approveMiniButton"
                          disabled={reviewBusyId === -group.id}
                          onClick={() => void approveWholeGroup(group)}
                        >
                          全部通过
                        </button>
                      ) : null}
```

替换为（分发只在「已审核」侧且整组已审核；全部通过只在「未审核」侧且有待审）：

```tsx
                      {reviewTab === "approved" && fullyApproved ? (
                        <button
                          type="button"
                          className="inlineMiniButton distributeMiniButton"
                          onClick={() => openDistributeForGroup(group)}
                        >
                          <Send size={13} />
                          自动分发
                        </button>
                      ) : reviewTab === "pending" && counts.total - counts.approved > 0 ? (
                        <button
                          type="button"
                          className="inlineMiniButton approveMiniButton"
                          disabled={reviewBusyId === -group.id}
                          onClick={() => void approveWholeGroup(group)}
                        >
                          全部通过
                        </button>
                      ) : null}
```

最后在展开区（约 1136-1137 行）`{isExpanded ? (` 之后、`<div className="groupRowArticles">` 内最前面加跨标签提示。把：

```tsx
                  {isExpanded ? (
                    <div className="groupRowArticles">
                      {groupArticles.map((article) => (
```

替换为：

```tsx
                  {isExpanded ? (
                    <div className="groupRowArticles">
                      {otherCount > 0 ? (
                        <p className="groupCrossTabHint">
                          另有 {otherCount} 篇{reviewTab === "pending" ? "已审核" : "待审核"}，
                          切到「{reviewTab === "pending" ? "已审核" : "未审核"}」标签查看
                        </p>
                      ) : null}
                      {groupArticles.map((article) => (
```

> 注意：`groupArticles.length === 0 ? <p className="emptyText">分组暂无文章</p> : null` 这行保留——当前标签无文章但有提示时会显示「分组暂无文章」，可接受；若想更干净可改为 `groupArticles.length === 0 && otherCount === 0` 才显示空文案。建议按后者改：把该行条件改为 `{groupArticles.length === 0 && otherCount === 0 ? <p className="emptyText">分组暂无文章</p> : null}`。

- [ ] **Step 5: 加样式**

`web/src/styles.css` 末尾追加：

```css
.groupCrossTabHint {
  margin: 0 0 6px;
  padding: 4px 8px;
  font-size: 12px;
  color: #64748b;
  background: #f1f5f9;
  border-radius: 6px;
}
```

- [ ] **Step 6: typecheck + build**

Run:
```bash
pnpm --filter @geo/web typecheck
pnpm --filter @geo/web build
```
Expected: 均通过、无 TS 报错。

> 若 `groupReviewTab` 还有其它引用导致「未使用 / 找不到」报错：全局搜 `groupReviewTab`，确认只在上面三处用过；已全部替换则无残留。

- [ ] **Step 7: 提交**

```bash
git add web/src/features/content/ContentWorkspace.tsx web/src/styles.css
git commit -m "feat(content): 日期分组未审/已审双标签可见 + 跨标签提示"
```

---

## Task 4：集成验收

- [ ] **Step 1: 后端全量门禁**

```bash
ruff check server/
ruff format --check server/
mypy server/app
<python> -m pytest server/tests/test_daily_grouping.py -q
```
Expected: 全绿。

- [ ] **Step 2: 前端门禁**

```bash
pnpm --filter @geo/web typecheck
pnpm --filter @geo/web build
```
Expected: 全绿。

- [ ] **Step 3: 自检对照设计文档 §10**

- [ ] 默认（`daily_group` 关）现有流水线行为零变化（`test_to_review_default_makes_new_group_each_run` 通过）。
- [ ] `daily_group` 开：同日累加、跨天新建、去重（T1/T2 测试通过）。
- [ ] 前端混合组双标签 + 跨标签提示；approve 不跳组（手动核对）。
- [ ] 后端测试 + 前端 typecheck/build 全绿。

---

## Self-Review（计划作者已核对）

- **Spec 覆盖**：§5.1→T1，§5.2/5.3→T2，§6→T3，§8→各任务测试步骤，§9→任务划分一致。
- **类型一致**：函数名 `mark_pending_and_append_daily` 在 T1 定义、T2 引用一致；config key `daily_group` 在 router/node/test/前端口径一致；前端 `groupHasStatus(group, status)` 签名在 3 处调用一致。
- **无占位符**：所有步骤含可运行代码/命令。
- **风险点**：`get_settings` 导入路径、`groupReviewTab` 残留引用——计划内已给 grep 兜底提示。
