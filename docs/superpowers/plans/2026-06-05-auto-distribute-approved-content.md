# 自动分发内容工作流 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 新增「已审核待发布 → 内容分发」分发型工作流：源节点从内容管理取"已审核且未分发过"的文章（自动去重），分发节点把它们 round-robin 分发到所选账号（单选或多选），配合定时调度实现无人值守的定时自动分发。

**Architecture:** tasks 引擎加性扩展 `task_type="article_round_robin"`（复用现有 round-robin 派号 + 审核门禁）；新增 `approved_content_source` 节点（查 approved + 未删 + 未分发过，去重）；`distribute` 节点支持消费上游 `article_ids`（空集安静跳过）。前端编辑器加 `checkbox` 字段类型。不新建表、不改参考项目。

**Tech Stack:** FastAPI + SQLAlchemy + MySQL + pytest（容器跑）；React 19 + Vite + TS（host pnpm）。

---

## 约定（与前序计划一致）

- **唯一改动目标 = geo-collab**；参考项目只读禁改。
- **后端在容器**：`docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest <args>'`。宿主无 python。`server/` bind-mount。**容器内若无 ruff 先 `pip install ruff -q`**。
- **ruff 双门禁**：`ruff check server/` 和 `ruff format --check server/`。测试 import 放顶部（E402）。
- **前端在 host**：`pnpm --filter @geo/web typecheck` + `build`。
- **错误**：service/node 抛 `ValidationError`/`ClientError`（`server.app.shared.errors`）。
- **后台线程 session**：节点自建 session、本线程 commit/close。
- **分支** `feat/auto-distribute-content`（基于 PR#28 分支，含其编辑器/node-types 改动，避免冲突）。逐 Task 提交。
- 现有事实（已核实，基于本分支）：
  - `tasks/schemas.py:TaskCreate{name,client_request_id,task_type,article_id,group_id,platform_code,accounts,stop_before_publish}`；`TaskAccountInput{account_id,sort_order}`。
  - `tasks/service.py`：`VALID_TASK_TYPES = {"single","group_round_robin"}`（line 25）；`create_task(db, user_id, payload, role=...)`；`_validated_task_inputs` 里 `payload.task_type not in VALID_TASK_TYPES`(442)、`task_type=="single" and len(ordered_accounts)!=1`(456)；`_article_ids_for_task(db, payload, user_id=None)` 按 task_type 分支取 ids；`_validate_unique_articles(article_ids)`；`_validate_articles_approved(db, article_ids)`（审核门禁）；`_build_assignments(article_ids, accounts)` 已按 article_ids round-robin 派号。
  - `tasks/models.py:PublishRecord.article_id`（每篇分发产生记录，用于去重）。
  - `articles/models.py:Article{id,user_id,review_status,is_deleted,updated_at}`。
  - `pipelines/nodes/distribute_node.py` 现只走 group_id 路径。
  - `pipelines/router.py:get_node_types()` 已含 distribute（accounts+name）等；本计划加 approved_content_source 一项。
  - `pipelines/nodes/base.py`：NodeRunContext/NodeResult/register。
  - 前端 `PipelineEditor.tsx` config 渲染链含 number/text/textarea/article_group/accounts/question_pool/question_type/ai_engine/prompt_templates；本计划加 `checkbox`。

---

## Task 1: tasks 引擎扩展 `article_round_robin`

**Files:**
- Modify: `server/app/modules/tasks/schemas.py`
- Modify: `server/app/modules/tasks/service.py`
- Test: `server/tests/test_auto_distribute.py`（新建）

- [ ] **Step 1: 写失败测试（@pytest.mark.mysql）**

```python
# server/tests/test_auto_distribute.py
import pytest

from server.tests.utils import build_test_app


def _make_approved_article(client, title="文章"):
    r = client.post("/api/articles", json={
        "title": title, "content_json": {"type": "doc", "content": []},
        "content_html": "<p>x</p>", "plain_text": "x", "word_count": 1, "status": "ready"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _make_account(app, client, key="acc1", name="账号1"):
    """参照 test_pipeline_review_distribute.py 的账号夹具：写 storage_state + 创建账号。"""
    import json as _json
    from pathlib import Path

    state_dir = Path(app.data_dir) / "browser_states" / "toutiao" / key
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "storage_state.json").write_text(_json.dumps({"cookies": [], "origins": []}))
    r = client.post("/api/accounts/toutiao/login", json={
        "display_name": name, "account_key": key, "use_browser": False})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


@pytest.mark.mysql
def test_article_round_robin_task_built(monkeypatch):
    from server.app.modules.tasks.models import PublishRecord, PublishTask
    from server.app.modules.tasks.schemas import TaskAccountInput, TaskCreate
    from server.app.modules.tasks.service import create_task

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a1, a2, a3 = (_make_approved_article(client, t) for t in ("甲", "乙", "丙"))
        acc1 = _make_account(app, client, "k1", "号1")
        acc2 = _make_account(app, client, "k2", "号2")
        with app.session_factory() as db:
            from server.app.modules.articles.models import Article
            uid = db.get(Article, a1).user_id
            tc = TaskCreate(
                name="自动分发", task_type="article_round_robin",
                article_ids=[a1, a2, a3],
                accounts=[TaskAccountInput(account_id=acc1, sort_order=0),
                          TaskAccountInput(account_id=acc2, sort_order=1)],
                stop_before_publish=False)
            task = create_task(db, uid, tc, role="admin")
            db.commit()
            tid = task.id
        with app.session_factory() as db:
            t = db.get(PublishTask, tid)
            assert t.task_type == "article_round_robin"
            recs = db.query(PublishRecord).filter(PublishRecord.task_id == tid).all()
            assert {r.article_id for r in recs} == {a1, a2, a3}  # 3 篇都派发
            assert len({r.account_id for r in recs}) == 2        # round-robin 到 2 账号
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_article_round_robin_empty_raises(monkeypatch):
    from server.app.modules.tasks.schemas import TaskAccountInput, TaskCreate
    from server.app.modules.tasks.service import create_task
    from server.app.shared.errors import ClientError

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        acc1 = _make_account(app, client)
        with app.session_factory() as db:
            from server.app.modules.system.models import User
            uid = db.query(User).first().id
            tc = TaskCreate(name="空", task_type="article_round_robin", article_ids=[],
                            accounts=[TaskAccountInput(account_id=acc1, sort_order=0)])
            with pytest.raises(ClientError):
                create_task(db, uid, tc, role="admin")
    finally:
        app.cleanup()
```

- [ ] **Step 2: 运行确认失败**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_auto_distribute.py -q -k article_round_robin'`
Expected: FAIL（TaskCreate 无 article_ids 字段 / task_type 非法）

- [ ] **Step 3: schemas — 加 article_ids 字段**

`schemas.py` 的 `TaskCreate` 加一行（在 `group_id` 之后）：
```python
    article_ids: list[int] | None = None  # 仅 article_round_robin 用
```
并把 `task_type` 注释改为 `# single / group_round_robin / article_round_robin`。

- [ ] **Step 4: service — 加 task_type + _article_ids_for_task 分支**

`service.py`：
```python
VALID_TASK_TYPES = {"single", "group_round_robin", "article_round_robin"}
```
在 `_article_ids_for_task` 里，`single` 分支之后、`group_round_robin` 逻辑之前，加：
```python
    if payload.task_type == "article_round_robin":
        ids = list(payload.article_ids or [])
        if not ids:
            raise ClientError("article_ids is required for article_round_robin task")
        rows = db.execute(
            select(Article.id, Article.user_id).where(
                Article.id.in_(ids),
                Article.is_deleted == False,  # noqa: E712
            )
        ).all()
        owner_by_id = {r[0]: r[1] for r in rows}
        for aid in ids:
            if aid not in owner_by_id or (user_id is not None and owner_by_id[aid] != user_id):
                raise ClientError(f"Article not found: {aid}")
        return ids
```
> `select` / `Article` 已在 service.py import（确认；group_round_robin 分支已用它们）。`article_round_robin` 不触发 `single` 的"恰好 1 账号"检查（那段是 `task_type=="single"`），所以 1..N 账号都允许。`_validate_unique_articles` 后续会拦重复 id（源节点已去重，正常不重复）。

- [ ] **Step 5: 运行通过 + ruff**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && pip install ruff -q 2>/dev/null; GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_auto_distribute.py -q && ruff check server/app/modules/tasks/ server/tests/test_auto_distribute.py && ruff format --check server/app/modules/tasks/ server/tests/test_auto_distribute.py'`
Expected: 2 passed + ruff clean。

- [ ] **Step 6: 提交**

```bash
git add server/app/modules/tasks/schemas.py server/app/modules/tasks/service.py server/tests/test_auto_distribute.py
git commit -m "feat(tasks): article_round_robin task type (distribute explicit article_ids)"
```

---

## Task 2: `approved_content_source` 节点（已审核待发布）

**Files:**
- Create: `server/app/modules/pipelines/nodes/approved_content_source.py`
- Modify: `server/app/modules/pipelines/nodes/__init__.py`
- Modify: `server/app/modules/pipelines/router.py`（node-types 增一项，label「已审核待发布」）
- Test: `server/tests/test_auto_distribute.py`（追加）

- [ ] **Step 1: 追加失败测试**

```python
@pytest.mark.mysql
def test_approved_content_source_dedup_and_filter(monkeypatch):
    from server.app.modules.articles.models import Article
    from server.app.modules.pipelines.nodes.approved_content_source import run_approved_content_source
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.tasks.models import PublishRecord

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a1 = _make_approved_article(client, "已审1")
        a2 = _make_approved_article(client, "已审2")
        a3 = _make_approved_article(client, "已审已发")
        # a3 标记为已分发（造一条 PublishRecord）；a4 设为 pending（不该被取）
        a4 = _make_approved_article(client, "未审")
        with app.session_factory() as db:
            uid = db.get(Article, a1).user_id
            db.get(Article, a4).review_status = "pending"
            db.add(PublishRecord(task_id=1, article_id=a3, platform_id=1, account_id=1, status="succeeded"))
            db.commit()
        ctx = NodeRunContext(session_factory=app.session_factory, user_id=uid,
                             config={"limit": 10, "exclude_distributed": True}, inputs={}, upstream={})
        res = run_approved_content_source(ctx)
        ids = set(res.output["article_ids"])
        assert a1 in ids and a2 in ids
        assert a3 not in ids   # 已分发被去重
        assert a4 not in ids   # pending 不取
        # exclude_distributed=False → a3 回来
        ctx2 = NodeRunContext(session_factory=app.session_factory, user_id=uid,
                              config={"limit": 10, "exclude_distributed": False}, inputs={}, upstream={})
        assert a3 in set(run_approved_content_source(ctx2).output["article_ids"])
    finally:
        app.cleanup()
```
> 注：`PublishRecord` 必填字段以 models.py 实际为准（task_id/article_id/platform_id/account_id/status）。若有其它 NOT NULL 字段，补上最小值。

- [ ] **Step 2: 运行确认失败** — `-k approved_content_source` → FAIL（ImportError）。

- [ ] **Step 3: 实现节点**

```python
# server/app/modules/pipelines/nodes/approved_content_source.py
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register


def run_approved_content_source(ctx: NodeRunContext) -> NodeResult:
    from sqlalchemy import select

    from server.app.modules.articles.models import Article
    from server.app.modules.system.models import User
    from server.app.modules.tasks.models import PublishRecord

    cfg = ctx.config or {}
    limit = int(cfg.get("limit") or 20)
    limit = max(1, min(limit, 200))
    exclude_distributed = cfg.get("exclude_distributed")
    exclude_distributed = True if exclude_distributed is None else bool(exclude_distributed)

    db = ctx.session_factory()
    try:
        user = db.get(User, ctx.user_id)
        is_admin = user is not None and user.role == "admin"
        stmt = select(Article.id).where(
            Article.review_status == "approved",
            Article.is_deleted == False,  # noqa: E712
        )
        if not is_admin:
            stmt = stmt.where(Article.user_id == ctx.user_id)
        if exclude_distributed:
            stmt = stmt.where(
                Article.id.notin_(select(PublishRecord.article_id).distinct())
            )
        stmt = stmt.order_by(Article.updated_at.desc()).limit(limit)
        article_ids = [r[0] for r in db.execute(stmt).all()]
    finally:
        db.close()

    return NodeResult(output={"article_ids": article_ids}, article_ids=[])


register("approved_content_source", run_approved_content_source)
```

- [ ] **Step 4: 注册 + node-types**

`nodes/__init__.py` 现为一个分组 import 元组（`from server.app.modules.pipelines.nodes import (ai_compose, ai_generate_node, article_group_source, base, distribute_node, input_node, question_source, to_review,)`）。把 `approved_content_source,  # noqa: F401` 加入该元组（按字母序放在 `ai_generate_node` 之后、`article_group_source` 之前）。**不要**新起一行单独 import。
`router.py:get_node_types()` 的 `node_types` 列表追加：
```python
            {"type": "approved_content_source", "label": "已审核待发布",
             "config_schema": [
                 {"key": "limit", "type": "number", "label": "取多少篇(默认20)"},
                 {"key": "exclude_distributed", "type": "checkbox", "label": "跳过已分发过的"},
             ]},
```

- [ ] **Step 5: 运行通过 + 注册校验 + ruff**

Run:
```
docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_auto_distribute.py -q && python -c "import server.app.modules.pipelines.nodes; from server.app.modules.pipelines.nodes.base import registered_types; print(registered_types())" && pip install ruff -q 2>/dev/null; ruff check server/app/modules/pipelines/ server/tests/test_auto_distribute.py && ruff format --check server/app/modules/pipelines/ server/tests/test_auto_distribute.py'
```
Expected: 测试全过；registered_types 含 `approved_content_source`；ruff clean。

- [ ] **Step 6: 提交**

```bash
git add server/app/modules/pipelines/nodes/approved_content_source.py server/app/modules/pipelines/nodes/__init__.py server/app/modules/pipelines/router.py server/tests/test_auto_distribute.py
git commit -m "feat(pipelines): approved_content_source node (已审核待发布, dedup) + node-types"
```

---

## Task 3: `distribute` 节点支持 article_ids

**Files:**
- Modify: `server/app/modules/pipelines/nodes/distribute_node.py`
- Test: `server/tests/test_auto_distribute.py`（追加）

- [ ] **Step 1: 追加失败测试**

```python
@pytest.mark.mysql
def test_distribute_consumes_article_ids_and_skips_empty(monkeypatch):
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.distribute_node import run_distribute
    from server.app.modules.tasks.models import PublishTask

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a1 = _make_approved_article(client, "x1")
        a2 = _make_approved_article(client, "x2")
        acc1 = _make_account(app, client, "ka", "甲号")
        with app.session_factory() as db:
            from server.app.modules.articles.models import Article
            uid = db.get(Article, a1).user_id
        # 有 article_ids → 建 article_round_robin 任务
        ctx = NodeRunContext(session_factory=app.session_factory, user_id=uid,
                             config={"account_ids": [acc1]},
                             inputs={"article_ids": [a1, a2]}, upstream={})
        res = run_distribute(ctx)
        assert res.output.get("task_id")
        with app.session_factory() as db:
            assert db.query(PublishTask).filter(PublishTask.task_type == "article_round_robin").count() == 1
        # 空 article_ids → 跳过、不建任务
        ctx_empty = NodeRunContext(session_factory=app.session_factory, user_id=uid,
                                   config={"account_ids": [acc1]},
                                   inputs={"article_ids": []}, upstream={})
        r2 = run_distribute(ctx_empty)
        assert r2.output.get("skipped")
        with app.session_factory() as db:
            assert db.query(PublishTask).count() == 1  # 没新建第二个
    finally:
        app.cleanup()
```

- [ ] **Step 2: 运行确认失败**（当前 distribute 只认 group_id，传 article_ids 会因缺 group_id 抛错）

- [ ] **Step 3: 改 distribute 节点**

```python
# server/app/modules/pipelines/nodes/distribute_node.py
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError


def run_distribute(ctx: NodeRunContext) -> NodeResult:
    from server.app.modules.system.models import User
    from server.app.modules.tasks.schemas import TaskAccountInput, TaskCreate
    from server.app.modules.tasks.service import create_task

    cfg = ctx.config or {}
    account_ids = cfg.get("account_ids") or []
    if not account_ids:
        raise ValidationError("distribute 节点需配置至少一个分发账号")

    # 优先消费上游 article_ids（已审核待发布源）；否则走旧 group_id 路径（兼容 article_group_source）
    article_ids = ctx.inputs.get("article_ids")
    group_id = ctx.inputs.get("group_id") or cfg.get("group_id")

    accounts = [TaskAccountInput(account_id=a, sort_order=i) for i, a in enumerate(account_ids)]

    if article_ids is not None:
        # 上游明确给了 article_ids（可能为空）
        if not article_ids:
            return NodeResult(output={"skipped": "无可分发内容"}, article_ids=[])
        name = cfg.get("name") or f"自动分发 {len(article_ids)} 篇"
        task_create = TaskCreate(
            name=name, task_type="article_round_robin",
            article_ids=list(article_ids), accounts=accounts, stop_before_publish=False,
        )
    elif group_id:
        name = cfg.get("name") or f"自动分发 分组 {group_id}"
        task_create = TaskCreate(
            name=name, task_type="group_round_robin",
            group_id=group_id, accounts=accounts, stop_before_publish=False,
        )
    else:
        raise ValidationError("distribute 节点缺少 article_ids（上游）或 group_id（配置）")

    db = ctx.session_factory()
    try:
        user = db.get(User, ctx.user_id)
        role = user.role if user is not None else "operator"
        # create_task 内部做审核门禁 + 账号校验，抛命名异常
        task = create_task(db, ctx.user_id, task_create, role=role)
        db.commit()
        task_id = task.id
    finally:
        db.close()

    return NodeResult(output={"task_id": task_id}, article_ids=[])


register("distribute", run_distribute)
```
> 注意保持现有审核分发用例（article_group_source→distribute 走 group_id）不破——本改动里 group_id 路径行为不变。

- [ ] **Step 4: 运行通过 + 回归（现有审核分发用例）**

Run:
```
docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_auto_distribute.py server/tests/test_pipeline_review_distribute.py -q && pip install ruff -q 2>/dev/null; ruff check server/app/modules/pipelines/nodes/distribute_node.py && ruff format --check server/app/modules/pipelines/nodes/distribute_node.py'
```
Expected: 全 PASS（含 `test_pipeline_review_distribute` 的 group_id 路径回归）+ ruff clean。

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/pipelines/nodes/distribute_node.py server/tests/test_auto_distribute.py
git commit -m "feat(pipelines): distribute consumes upstream article_ids (skip empty), keeps group_id path"
```

---

## Task 4: 前端 `checkbox` 配置字段类型

**Files:**
- Modify: `web/src/features/pipelines/PipelineEditor.tsx`

- [ ] **Step 1: 读现有 config 字段渲染链**

Run: `grep -n "f.type === " web/src/features/pipelines/PipelineEditor.tsx | head` 定位渲染分支链（number/text/textarea/article_group/accounts/...）。

- [ ] **Step 2: 加 checkbox 分支**

在 config 字段渲染的三元链里（放在通用 number/text 之前，与其它新分支并列）加：
```tsx
{f.type === "checkbox" ? (
  <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
    <input type="checkbox" checked={!!sel.config[f.key]}
      onChange={(e) => updateNode(selected!, { config: { ...sel.config, [f.key]: e.target.checked } })} />
    <span className="agentHint">{f.label}</span>
  </label>
) : /* 现有其它分支... */}
```
> checkbox 的 `f.label` 由分支内的 span 显示；外层 `.agentFieldLabel`（label 上方小标题）对 checkbox 冗余——实现时若外层已渲染 label，可让 checkbox 分支的 span 省略或保留，保持视觉整洁，二选一即可（不影响功能）。

- [ ] **Step 3: typecheck + build**

Run（host）：`pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 通过。

- [ ] **Step 4: 提交**

```bash
git add web/src/features/pipelines/PipelineEditor.tsx
git commit -m "feat(pipelines): checkbox config field type in node editor"
```

---

## Task 5: 端到端集成测试（已审核待发布 → 分发 + 去重）

**Files:**
- Modify: `server/tests/test_auto_distribute.py`（追加端到端）

- [ ] **Step 1: 追加端到端测试**

```python
@pytest.mark.mysql
def test_end_to_end_approved_to_distribute_dedup(monkeypatch):
    from server.app.modules.pipelines.executor import create_run, run_pipeline
    from server.app.modules.pipelines.models import Pipeline
    from server.app.modules.tasks.models import PublishRecord, PublishTask

    app = build_test_app(monkeypatch)
    client = app.client
    try:
        a1 = _make_approved_article(client, "审1")
        a2 = _make_approved_article(client, "审2")
        acc1 = _make_account(app, client, "kk", "号")
        snap = {"schemaVersion": 1, "nodes": [
            {"node_type": "approved_content_source", "name": "已审核待发布", "node_index": 0,
             "config": {"limit": 50, "exclude_distributed": True}, "flow_meta": None},
            {"node_type": "distribute", "name": "内容分发", "node_index": 1,
             "config": {"account_ids": [acc1]},
             "flow_meta": {"inputMapping": [{"from": "article_ids", "to": "article_ids"}]}},
        ]}
        pid = client.post("/api/pipelines", json={"name": "自动分发智能体", "type": "distribution"}).json()["id"]
        client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snap})
        client.post(f"/api/pipelines/{pid}/publish", json={})

        def _run():
            with app.session_factory() as db:
                p = db.get(Pipeline, pid)
                run = create_run(db, pipeline_id=pid, user_id=p.user_id); db.commit(); rid = run.id
            run_pipeline(rid, app.session_factory)
            return client.get(f"/api/pipelines/runs/{rid}").json()

        # 第一次：建任务，覆盖 a1/a2
        r1 = _run()
        assert r1["status"] == "done", r1
        with app.session_factory() as db:
            assert db.query(PublishTask).filter(PublishTask.task_type == "article_round_robin").count() == 1
            distributed = {rec.article_id for rec in db.query(PublishRecord).all()}
            assert {a1, a2}.issubset(distributed)
        # 第二次：a1/a2 已分发 → 源去重为空 → distribute 跳过 → run done、不建第二个任务
        r2 = _run()
        assert r2["status"] == "done", r2
        with app.session_factory() as db:
            assert db.query(PublishTask).count() == 1  # 仍只有 1 个
    finally:
        app.cleanup()
```

- [ ] **Step 2: 运行全文件 + ruff**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_auto_distribute.py -q && pip install ruff -q 2>/dev/null; ruff check server/tests/test_auto_distribute.py && ruff format --check server/tests/test_auto_distribute.py'`
Expected: 全 PASS（article_round_robin / 源去重 / distribute / 端到端）+ ruff clean。

- [ ] **Step 3: 提交**

```bash
git add server/tests/test_auto_distribute.py
git commit -m "test(pipelines): end-to-end approved_content_source -> distribute with dedup"
```

---

## Self-Review 结果

- **Spec 覆盖**：§3 源节点=Task2;§4.1 tasks article_round_robin=Task1;§4.2 distribute=Task3;§5 前端=Task4(checkbox)+Task2(node-types);§6 测试=各 Task + Task5 端到端;§9 验收 1=Task2(去重),2=Task3/Task5,3=Task3 空集跳过,4=门禁(create_task 内 `_validate_articles_approved`,已有,Task1/Task3 路径覆盖)。
- **占位符**：无 TBD;每步完整代码;对未读精确处（PublishRecord 必填字段、service.py 是否已 import select/Article、PipelineEditor 渲染链分支位置、accounts 字段已存在）给"先确认"指令。
- **类型一致**：`TaskCreate{...,article_ids}`、`task_type="article_round_robin"`、`_article_ids_for_task` 分支跨 Task1/Task3 一致；节点 `NodeRunContext/NodeResult` 一致；`approved_content_source` 输出 `{article_ids}` → distribute 经 inputMapping 读 `article_ids` 一致；node-types `type` 字符串 `approved_content_source` 与 register、前端无新字段类型冲突（checkbox 为新增）。
- **门禁双保险**：distribute 走 create_task → `_validate_articles_approved`；源节点只取 approved，正常不会给未审；若人工硬塞未审 article_ids，create_task 门禁拦截、节点失败、run failed（与现有 distribute 行为一致）。
- **待核对点（执行时）**：PublishRecord 必填列、service.py import（select/Article）、PipelineEditor 渲染链插入点、`/api/accounts/toutiao/login` 夹具字段（参照 test_pipeline_review_distribute.py）。
