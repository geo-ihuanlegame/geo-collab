# 「已审核分组源」自动选组优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 让 `article_group_source` 节点的 `group_id` 可选：留空时自动按 FIFO 选「最早一个含 已审核+未分发 文章」的分组，并只输出该组 approved+未分发 文章为 `article_ids`，配合 `article_round_robin` 实现定时逐批自动分发。

**Architecture:** 纯读侧逻辑改造单个节点 + 一处 node-types label。复用 #34 的 `distribute` article_ids 路径与 `PublishRecord` 去重判定。不新建表/列、不改 distribute、不改审核模型。

**Tech Stack:** FastAPI + SQLAlchemy + MySQL + pytest（容器跑）；React 19 + Vite（仅 build 验证，无代码改动）。

---

## 约定

- **唯一改动目标 = geo-collab**；参考项目只读禁改。
- **后端在容器**：`docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest <args>'`。宿主无 python。`server/` bind-mount。容器内若无 ruff：`pip install ruff -q`。
- **ruff 双门禁**：`ruff check` + `ruff format --check`。
- **前端在 host**：`pnpm --filter @geo/web build`（本特性前端无代码改动，仅确认 build 不受影响）。
- **分支** `feat/group-source-auto`（已基于 `feat/auto-distribute`＝#34 建好，spec 已提交 cb86597）。逐 Task 提交。#34 合并后由控制者把本分支 rebase 到最新 main 再开 PR。
- 已核实事实（基于本分支）：
  - 现 `article_group_source.py`：必填 `group_id`，输出 `{group_id, article_ids(全部未删)}`。
  - `Article{id,user_id,review_status,is_deleted}`，`review_status` 默认 `"approved"`，pending 由 `mark_pending_and_group` 设。
  - `ArticleGroup{id,user_id,name,is_deleted,created_at,...}`；`ArticleGroupItem{group_id,article_id,sort_order}`。
  - `PublishRecord.article_id`：NOT NULL，有索引 → `NOT IN (select article_id ...)` 安全。
  - `NodeRunContext{session_factory,user_id,config,inputs,upstream}`；`NodeResult(output, article_ids)`；`register` 在 `nodes/base.py`。
  - `get_node_types()`（`pipelines/router.py`）含 `{"type":"article_group_source","label":"已审核分组源","config_schema":[{"key":"group_id","type":"article_group","label":"内容分组"}]}`。
  - `distribute` 节点（#34）：优先 `ctx.inputs["article_ids"]`（非空→article_round_robin，空→skip），否则 group_id→group_round_robin。
  - `test_auto_distribute.py` 已有 `_make_account(app, client, key, name)` + `_make_approved_article(client, title)` 风格的 helper 与「用 `create_task(article_round_robin)` 制造 PublishRecord」的做法——新测试可镜像它来标记"已分发"。

---

## Task 1: 重写 `article_group_source` 节点（自动选组 + 已审未发子集）

**Files:**
- Modify: `server/app/modules/pipelines/nodes/article_group_source.py`（整体替换 `run_article_group_source`）
- Modify: `server/app/modules/pipelines/router.py`（`get_node_types()` 改 group_id 字段 label）
- Test: `server/tests/test_group_source_auto.py`（新建）

- [ ] **Step 1: 写失败测试（@pytest.mark.mysql）**

```python
# server/tests/test_group_source_auto.py
import pytest

from server.tests.utils import build_test_app


def _make_approved_article(client, title="文章"):
    r = client.post("/api/articles", json={
        "title": title, "content_json": {"type": "doc", "content": []},
        "content_html": "<p>x</p>", "plain_text": "x", "word_count": 1, "status": "ready"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _make_group(app, uid, name, article_ids):
    """直接建分组 + 组项（顺序即 sort_order）。返回 group_id。"""
    from server.app.modules.articles.models import ArticleGroup, ArticleGroupItem

    with app.session_factory() as db:
        g = ArticleGroup(user_id=uid, name=name)
        db.add(g)
        db.flush()
        for i, aid in enumerate(article_ids):
            db.add(ArticleGroupItem(group_id=g.id, article_id=aid, sort_order=i))
        db.commit()
        return g.id


def _set_pending(app, article_id):
    from server.app.modules.articles.models import Article

    with app.session_factory() as db:
        db.get(Article, article_id).review_status = "pending"
        db.commit()


def _make_account(app, client, key="acc1", name="账号1"):
    """镜像 test_auto_distribute.py 的账号夹具：写 storage_state + /api/accounts/toutiao/login。"""
    import json as _json
    from pathlib import Path

    state_dir = Path(app.data_dir) / "browser_states" / "toutiao" / key
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "storage_state.json").write_text(_json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    r = client.post("/api/accounts/toutiao/login", json={
        "display_name": name, "account_key": key, "use_browser": False})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _mark_distributed(app, client, article_ids, key="dk"):
    """用 create_task(article_round_robin) 给文章产出 PublishRecord，标记已分发。"""
    from server.app.modules.articles.models import Article
    from server.app.modules.tasks.schemas import TaskAccountInput, TaskCreate
    from server.app.modules.tasks.service import create_task

    acc = _make_account(app, client, key=key, name=key)
    with app.session_factory() as db:
        uid = db.get(Article, article_ids[0]).user_id
        create_task(db, uid, TaskCreate(
            name="标记已分发", task_type="article_round_robin", article_ids=list(article_ids),
            accounts=[TaskAccountInput(account_id=acc, sort_order=0)]), role="admin")
        db.commit()


def _uid(app):
    from server.app.modules.system.models import User

    with app.session_factory() as db:
        return db.query(User).first().id


def _run_node(app, uid, config):
    from server.app.modules.pipelines.nodes.article_group_source import run_article_group_source
    from server.app.modules.pipelines.nodes.base import NodeRunContext

    return run_article_group_source(NodeRunContext(
        session_factory=app.session_factory, user_id=uid, config=config, inputs={}, upstream={}))


@pytest.mark.mysql
def test_auto_picks_oldest_group_with_candidates(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        uid = _uid(app)
        a1, a2 = _make_approved_article(client, "甲"), _make_approved_article(client, "乙")
        b1 = _make_approved_article(client, "丙")
        g1 = _make_group(app, uid, "早组", [a1, a2])
        _make_group(app, uid, "晚组", [b1])
        res = _run_node(app, uid, {})  # 自动模式
        assert res.output["group_id"] == g1
        assert res.output["article_ids"] == [a1, a2]  # 按 sort_order
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_only_approved_undistributed_subset(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        uid = _uid(app)
        a_ok = _make_approved_article(client, "已审未发")
        a_sent = _make_approved_article(client, "已审已发")
        a_pending = _make_approved_article(client, "未审")
        g = _make_group(app, uid, "混合组", [a_ok, a_sent, a_pending])
        _set_pending(app, a_pending)
        _mark_distributed(app, client, [a_sent])
        res = _run_node(app, uid, {"group_id": g})  # 手动选该组
        assert res.output["group_id"] == g
        assert res.output["article_ids"] == [a_ok]  # 只剩已审+未发
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_auto_skips_group_without_candidates(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        uid = _uid(app)
        a1 = _make_approved_article(client, "早-已发")
        b1 = _make_approved_article(client, "晚-未发")
        g1 = _make_group(app, uid, "早组", [a1])
        g2 = _make_group(app, uid, "晚组", [b1])
        _mark_distributed(app, client, [a1])  # 早组全部已分发 → 无候选
        res = _run_node(app, uid, {})
        assert res.output["group_id"] == g2  # 跳过 g1，选 g2
        assert res.output["article_ids"] == [b1]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_auto_empty_when_no_candidate_group(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        uid = _uid(app)
        a1 = _make_approved_article(client, "已发")
        _make_group(app, uid, "组", [a1])
        _mark_distributed(app, client, [a1])
        res = _run_node(app, uid, {})
        assert res.output["group_id"] is None
        assert res.output["article_ids"] == []
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_manual_invalid_group_raises(monkeypatch):
    from server.app.shared.errors import ValidationError

    app = build_test_app(monkeypatch)
    try:
        uid = _uid(app)
        with pytest.raises(ValidationError):
            _run_node(app, uid, {"group_id": 999999})
    finally:
        app.cleanup()
```

- [ ] **Step 2: 运行确认失败**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_group_source_auto.py -q'`
Expected: 多数 FAIL（现节点必填 group_id、输出全部未删文章、无自动选组）。`test_manual_invalid_group_raises` 可能已 PASS（现节点对不存在分组也抛 ValidationError）。

- [ ] **Step 3: 重写节点**

整体替换 `server/app/modules/pipelines/nodes/article_group_source.py` 的 `run_article_group_source`：
```python
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError


def run_article_group_source(ctx: NodeRunContext) -> NodeResult:
    from sqlalchemy import select

    from server.app.modules.articles.models import Article, ArticleGroup, ArticleGroupItem
    from server.app.modules.system.models import User
    from server.app.modules.tasks.models import PublishRecord

    cfg = ctx.config or {}
    # group_id 可选：上游注入 > 节点配置 > 空(=自动按 FIFO 选组)
    configured_group_id = ctx.inputs.get("group_id") or cfg.get("group_id")

    db = ctx.session_factory()
    try:
        user = db.get(User, ctx.user_id)
        is_admin = user is not None and user.role == "admin"

        # 候选文章 = 已审核 + 未删 + 未分发(无 PublishRecord) + owner/admin
        def _candidate_filters(stmt):
            stmt = stmt.where(
                Article.review_status == "approved",
                Article.is_deleted == False,  # noqa: E712
                Article.id.notin_(select(PublishRecord.article_id)),
            )
            if not is_admin:
                stmt = stmt.where(Article.user_id == ctx.user_id)
            return stmt

        if configured_group_id:
            group = db.get(ArticleGroup, configured_group_id)
            if group is None or group.is_deleted:
                raise ValidationError("分组不存在")
            if group.user_id != ctx.user_id and not is_admin:
                raise ValidationError("无权访问该分组")
            chosen_group_id = configured_group_id
        else:
            # 自动 FIFO：含 ≥1 篇候选文章、未删、owner/admin 的最早分组
            grp_stmt = (
                select(ArticleGroup.id)
                .join(ArticleGroupItem, ArticleGroupItem.group_id == ArticleGroup.id)
                .join(Article, Article.id == ArticleGroupItem.article_id)
                .where(ArticleGroup.is_deleted == False)  # noqa: E712
            )
            grp_stmt = _candidate_filters(grp_stmt)
            if not is_admin:
                grp_stmt = grp_stmt.where(ArticleGroup.user_id == ctx.user_id)
            grp_stmt = grp_stmt.order_by(ArticleGroup.created_at.asc(), ArticleGroup.id.asc()).limit(1)
            chosen_group_id = db.execute(grp_stmt).scalars().first()

        if chosen_group_id is None:
            return NodeResult(output={"group_id": None, "article_ids": []}, article_ids=[])

        art_stmt = (
            select(ArticleGroupItem.article_id)
            .join(Article, Article.id == ArticleGroupItem.article_id)
            .where(ArticleGroupItem.group_id == chosen_group_id)
        )
        art_stmt = _candidate_filters(art_stmt)
        art_stmt = art_stmt.order_by(ArticleGroupItem.sort_order.asc())
        article_ids = list(db.execute(art_stmt).scalars().all())
    finally:
        db.close()

    return NodeResult(
        output={"group_id": chosen_group_id, "article_ids": article_ids}, article_ids=[]
    )


register("article_group_source", run_article_group_source)
```

- [ ] **Step 4: 改 node-types label**

`router.py:get_node_types()` 里 `article_group_source` 的 group_id 字段：
```python
{"key": "group_id", "type": "article_group", "label": "内容分组（留空＝自动选最早未分发分组）"},
```

- [ ] **Step 5: 运行通过 + ruff**

Run:
```
docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_group_source_auto.py -q && pip install ruff -q 2>/dev/null; ruff check server/app/modules/pipelines/nodes/article_group_source.py server/app/modules/pipelines/router.py server/tests/test_group_source_auto.py && ruff format --check server/app/modules/pipelines/nodes/article_group_source.py server/app/modules/pipelines/router.py server/tests/test_group_source_auto.py'
```
Expected: 5 passed + ruff clean。

- [ ] **Step 6: 提交**

```bash
git add server/app/modules/pipelines/nodes/article_group_source.py server/app/modules/pipelines/router.py server/tests/test_group_source_auto.py
git commit -m "feat(pipelines): article_group_source 自动选组(FIFO)+只发已审未发子集"
```

---

## Task 2: 回归 + 前端 build 验证

**Files:** 无（仅验证）

- [ ] **Step 1: 现有 review/distribute 回归（group_id→group_round_robin 整组路径不受影响）**

Run:
```
docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_pipeline_review_distribute.py server/tests/test_auto_distribute.py server/tests/test_group_source_auto.py -q'
```
Expected: 全 PASS。若 `test_pipeline_review_distribute.py` 某用例因节点 article_ids 输出收窄而失败：检查该用例是否映射 `article_ids`（应映射 `group_id`）；若确为映射 group_id 仍失败，STOP 并报告（说明对向后兼容判断有误）。

- [ ] **Step 2: 前端 build（label 变更来自后端，无前端代码改动，确认不受影响）**

Run（host）：`pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 通过。

- [ ] **Step 3: （可选）live 烟雾**

若 5173/8000 在跑：node-types 应返回 `article_group_source` 的 group_id label 含「留空＝自动选最早未分发分组」。

> 本任务无代码改动、无需提交。

---

## Self-Review 结果

- **Spec 覆盖**：§4 节点新行为（可选 group_id + 自动 FIFO + 候选子集 + 空跳过 + 手动校验）→ Task1 Step3 + 测试 1-5；§5 node-types label → Task1 Step4；§5 前端零改动 → Task2 Step2；§6 测试（自动FIFO/子集/跳过无候选组/空/手动校验/回归）→ Task1 测试 + Task2 Step1；§7 验收 1-5 → 各 Task 覆盖。
- **占位符**：无 TBD；每步完整代码；唯一"先确认"＝Task2 回归对向后兼容的判断（若 group_id 映射用例失败则 STOP）。
- **类型一致**：节点输出 `{group_id, article_ids}` 与 distribute 消费一致；测试用 `NodeRunContext`/`run_article_group_source` 与节点签名一致；`_candidate_filters` 在自动选组与取文章两处复用同一过滤，保证"选中的组一定有候选、取出的就是候选"自洽。
- **去重安全**：`PublishRecord.article_id` NOT NULL，`NOT IN` 安全（与 approved_content_source 同款判定）。
- **向后兼容**：手动模式 `group_id` 输出＝配置值，现有 `group_id→group_round_robin` 不变；仅 `article_ids` 输出收窄（只影响映射 article_ids 的流程，更正确）。
