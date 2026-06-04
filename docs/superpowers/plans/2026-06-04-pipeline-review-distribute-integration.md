# 编排引擎打通审核 + 分发 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 在已有 pipelines 引擎上：(A) pipeline 产出文章自动 pending+成组进审核库；(B) 新增 `article_group_source` + `distribute` 节点完成「已审核分组→分发」工作流。全部复用 PR #19 机制，不新建表。

**Architecture:** Track A 在 `articles/service.py` 加可复用 `mark_pending_and_group`，`pipelines/executor.py:run_pipeline` 末尾 best-effort 调用。Track B 加两个节点（复用 `tasks/service.create_task` 的 round-robin + 审核门禁）。前端属性面板加 `article_group`/`accounts` 两种 config 字段类型。

**Tech Stack:** FastAPI + SQLAlchemy + MySQL + pytest（后端在容器跑）；React 19 + Vite + TS（前端在 host 跑 pnpm）。

---

## 约定（沿用上一计划，复述关键点）

- **唯一改动目标 = geo-collab 主仓库**；`content-library-public` / `pc-admin-conetnt-library-public` 只读，禁止编辑。
- **后端测试在容器**：`docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest <args>'`。宿主无 python。`server/` 是 bind-mount，host 编辑实时生效。
- **ruff 双门禁**：`ruff check server/` **和** `ruff format --check server/` 都要过。`Callable` 从 `collections.abc` 导入（UP035）。测试文件所有 import 放顶部（E402）。
- **前端在 host**：`pnpm --filter @geo/web typecheck` + `pnpm --filter @geo/web build`，两者皆硬门禁。
- **错误**：service/node 抛 `ClientError`/`ValidationError`（`server.app.shared.errors`），不抛裸 ValueError。
- **后台线程 session**：每个 node/helper 自建 session、本线程 commit+close，不跨线程传 session。
- **分支** `feat/pipeline-review-distribute`，逐 Task 提交。
- 复用点（已核实）：`tasks/service.py:create_task(db, user_id, TaskCreate, role=...)`（内部审核门禁+账号校验）；`tasks/schemas.py:TaskCreate`/`TaskAccountInput`；`articles/models.py:Article/ArticleGroup/ArticleGroupItem`；`ai_generation/scheme_executor.py:_group_run_articles`（镜像对象）；`pipelines/nodes/base.py`（NodeRunContext/NodeResult/register/get_handler）；`pipelines/executor.py:run_pipeline`；前端 `web/src/api/articles.ts:listArticleGroups()`、`web/src/api/accounts.ts:listAccounts()`。

---

## Task 1: `mark_pending_and_group` helper（articles/service.py）

**Files:**
- Modify: `server/app/modules/articles/service.py`
- Test: `server/tests/test_pipeline_review_distribute.py`（新建，先放本 helper 的集成测试）

- [ ] **Step 1: 先读现有模式**

Run: `grep -n "class ArticleGroup\|class ArticleGroupItem\|sort_order\|is_deleted\|user_id\|name" server/app/modules/articles/models.py | head -30`
确认 `ArticleGroup(id,user_id,name,is_deleted)` 与 `ArticleGroupItem(group_id,article_id,sort_order)` 字段名。再读 `ai_generation/scheme_executor.py:245-328`（`_group_run_articles`）作为镜像参考。

- [ ] **Step 2: 写失败测试**（@pytest.mark.mysql）

```python
# server/tests/test_pipeline_review_distribute.py
import pytest

from server.tests.utils import build_test_app


def _make_article(client, title="文章") -> int:
    resp = client.post("/api/articles", json={
        "title": title, "content_json": {"type": "doc", "content": []},
        "content_html": "<p>x</p>", "plain_text": "x", "word_count": 1, "status": "ready",
    })
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


@pytest.mark.mysql
def test_mark_pending_and_group_sets_pending_and_groups(monkeypatch):
    from server.app.modules.articles.models import Article, ArticleGroup, ArticleGroupItem
    from server.app.modules.articles.service import mark_pending_and_group

    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        a1 = _make_article(client, "甲")
        a2 = _make_article(client, "乙")
        # 新建文章默认 approved；helper 应翻成 pending 并成组
        with test_app.session_factory() as db:
            uid = db.query(Article).first().user_id
        gid = mark_pending_and_group(
            test_app.session_factory, article_ids=[a1, a2], user_id=uid, base_name="测试组"
        )
        assert gid is not None
        with test_app.session_factory() as db:
            assert db.get(Article, a1).review_status == "pending"
            assert db.get(Article, a2).review_status == "pending"
            grp = db.get(ArticleGroup, gid)
            assert grp is not None and grp.name == "测试组"
            items = db.query(ArticleGroupItem).filter(ArticleGroupItem.group_id == gid).all()
            assert {it.article_id for it in items} == {a1, a2}
    finally:
        test_app.cleanup()
```

- [ ] **Step 3: 运行，确认失败**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_pipeline_review_distribute.py::test_mark_pending_and_group_sets_pending_and_groups -q'`
Expected: FAIL（ImportError: mark_pending_and_group）

- [ ] **Step 4: 实现 helper**（追加到 `articles/service.py` 末尾）

```python
import logging as _logging

_logger = _logging.getLogger(__name__)


def mark_pending_and_group(
    session_factory, *, article_ids: list[int], user_id: int, base_name: str
) -> int | None:
    """把文章标 review_status='pending' 并归入一个新 ArticleGroup（名 base_name）。
    撞 (user_id, name) 唯一约束时追加后缀。best-effort：失败记日志、不抛。
    用独立 session、本函数内 commit+close。返回 group_id 或 None。
    镜像 ai_generation.scheme_executor._group_run_articles。"""
    if not article_ids:
        return None
    try:
        from sqlalchemy.exc import IntegrityError

        from server.app.modules.articles.models import Article, ArticleGroup, ArticleGroupItem

        db = session_factory()
        try:
            for aid in article_ids:
                art = db.get(Article, aid)
                if art is not None:
                    art.review_status = "pending"

            exists = (
                db.query(ArticleGroup.id)
                .filter(
                    ArticleGroup.user_id == user_id,
                    ArticleGroup.name == base_name,
                    ArticleGroup.is_deleted.is_(False),
                )
                .first()
            )
            name = f"{base_name} #{article_ids[0]}" if exists is not None else base_name
            group = ArticleGroup(user_id=user_id, name=name)
            db.add(group)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                for aid in article_ids:
                    art = db.get(Article, aid)
                    if art is not None:
                        art.review_status = "pending"
                group = ArticleGroup(user_id=user_id, name=f"{base_name} #{article_ids[0]}")
                db.add(group)
                db.flush()

            for idx, aid in enumerate(article_ids):
                db.add(ArticleGroupItem(group_id=group.id, article_id=aid, sort_order=idx))
            gid = group.id
            db.commit()
            return gid
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — best-effort
        _logger.exception("mark_pending_and_group failed (user=%s, n=%s)", user_id, len(article_ids))
        return None
```
> 若 `articles/service.py` 顶部已 import `logging` / 有 module logger，复用它，删掉这里的 `_logging`/`_logger`（先 grep 确认，避免重复）。`ArticleGroupItem` 字段名以 Step 1 实际为准。

- [ ] **Step 5: 运行，确认通过 + ruff**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_pipeline_review_distribute.py -q && ruff check server/app/modules/articles/service.py server/tests/test_pipeline_review_distribute.py && ruff format --check server/app/modules/articles/service.py server/tests/test_pipeline_review_distribute.py'`
Expected: 1 passed + ruff clean（format 不过则先 `ruff format` 再提交）。

- [ ] **Step 6: 提交**

```bash
git add server/app/modules/articles/service.py server/tests/test_pipeline_review_distribute.py
git commit -m "feat(articles): mark_pending_and_group helper (reusable pending+grouping)"
```

---

## Task 2: executor 接入 Track A

**Files:**
- Modify: `server/app/modules/pipelines/executor.py`
- Test: `server/tests/test_pipeline_review_distribute.py`（追加 Track A 端到端）

- [ ] **Step 1: 追加失败测试**（建 input→ai_generate pipeline，run 后断言文章 pending+成组）

```python
# 追加到 server/tests/test_pipeline_review_distribute.py（import 放文件顶部）
def _make_generation_template(client) -> int:
    r = client.post("/api/prompt-templates", json={
        "name": "模板", "content": "写：", "scope": "generation"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


@pytest.mark.mysql
def test_pipeline_run_marks_articles_pending_and_groups(monkeypatch):
    from server.app.modules.articles.models import Article, ArticleGroupItem

    # 让 ai_generate 真造文章（默认 approved），返回其 id
    def _fake_generate(*, session_factory, user_id, template_content, question_text, model=None):
        from server.app.modules.articles.schemas import ArticleCreate
        from server.app.modules.articles.service import create_article
        import uuid
        db = session_factory()
        try:
            art = create_article(db, user_id, ArticleCreate(
                title="AI", content_json={"type": "doc", "content": []},
                content_html="<p>a</p>", plain_text="a", word_count=1,
                client_request_id=str(uuid.uuid4())))
            db.commit()
            return art.id
        finally:
            db.close()

    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_generate_node.generate_article_from_prompt",
        _fake_generate)
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        tpl = _make_generation_template(client)
        pid = client.post("/api/pipelines", json={"name": "生成流"}).json()["id"]
        snapshot = {"schemaVersion": 1, "nodes": [
            {"node_type": "input", "name": "源", "node_index": 0,
             "config": {"question_text": "主题"}, "flow_meta": None},
            {"node_type": "ai_generate", "name": "生文", "node_index": 1,
             "config": {"prompt_template_id": tpl, "count": 2},
             "flow_meta": {"inputMapping": [{"from": "question_text", "to": "question_text"}]}},
        ]}
        client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snapshot})
        client.post(f"/api/pipelines/{pid}/publish", json={})

        from server.app.modules.pipelines.executor import create_run, run_pipeline
        from server.app.modules.pipelines.models import Pipeline
        with test_app.session_factory() as db:
            p = db.get(Pipeline, pid)
            run = create_run(db, pipeline_id=p.id, user_id=p.user_id)
            db.commit(); run_id = run.id
        run_pipeline(run_id, test_app.session_factory)

        run = client.get(f"/api/pipelines/runs/{run_id}").json()
        assert run["status"] == "done"
        assert len(run["article_ids"]) == 2
        with test_app.session_factory() as db:
            for aid in run["article_ids"]:
                assert db.get(Article, aid).review_status == "pending"
            grouped = db.query(ArticleGroupItem).filter(
                ArticleGroupItem.article_id.in_(run["article_ids"])).all()
            assert len(grouped) == 2  # 都进了某个分组
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 运行，确认失败**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_pipeline_review_distribute.py::test_pipeline_run_marks_articles_pending_and_groups -q'`
Expected: FAIL（文章仍 approved / 未成组）

- [ ] **Step 3: executor 接入**

在 `pipelines/executor.py:run_pipeline` 的**末尾**（写回 run status 的那段 `finally`/commit 之后，函数返回前）追加 best-effort 块：
```python
    # Track A: 产出文章 → pending + 成组（best-effort，不影响 run 状态）
    if article_ids:
        try:
            from server.app.modules.articles.service import mark_pending_and_group
            from server.app.modules.pipelines.models import Pipeline

            db = session_factory()
            try:
                run = db.get(PipelineRun, run_id)
                p = db.get(Pipeline, run.pipeline_id) if run is not None else None
                pname = p.name if p is not None else f"工作流 {run_id}"
                created = run.created_at if run is not None else None
                base_name = (
                    f"{created:%Y/%m/%d %H:%M} · {pname}" if created else f"{pname} #{run_id}"
                )
                uid = run.user_id if run is not None else None
            finally:
                db.close()
            if uid is not None:
                mark_pending_and_group(
                    session_factory, article_ids=article_ids, user_id=uid, base_name=base_name
                )
        except Exception:  # noqa: BLE001
            logger.exception("pipeline run %s post-grouping failed", run_id)
```
> `article_ids` / `logger` 已是 `run_pipeline` 内现有变量。注意：这段在 run 状态已持久化之后执行，所以即使失败 run 仍是 done/partial_failed/failed。

- [ ] **Step 4: 运行，确认通过 + ruff（含 format）**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_pipeline_review_distribute.py -q && ruff check server/app/modules/pipelines/executor.py && ruff format --check server/app/modules/pipelines/executor.py'`
Expected: 2 passed + ruff clean。

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/pipelines/executor.py server/tests/test_pipeline_review_distribute.py
git commit -m "feat(pipelines): post-run mark generated articles pending + group (Track A)"
```

---

## Task 3: `article_group_source` + `distribute` 节点

**Files:**
- Create: `server/app/modules/pipelines/nodes/article_group_source.py`
- Create: `server/app/modules/pipelines/nodes/distribute_node.py`
- Modify: `server/app/modules/pipelines/nodes/__init__.py`
- Modify: `server/app/modules/pipelines/router.py`（`/node-types` 增补两项）

- [ ] **Step 1: article_group_source 节点**

```python
# server/app/modules/pipelines/nodes/article_group_source.py
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError


def run_article_group_source(ctx: NodeRunContext) -> NodeResult:
    from server.app.modules.articles.models import Article, ArticleGroup, ArticleGroupItem

    group_id = (ctx.config or {}).get("group_id")
    if not group_id:
        raise ValidationError("article_group_source 节点需配置 group_id")

    db = ctx.session_factory()
    try:
        group = db.get(ArticleGroup, group_id)
        if group is None or group.is_deleted:
            raise ValidationError("分组不存在")
        if group.user_id != ctx.user_id:
            # admin 放行（与其它模块一致：role 需从 user 取）
            from server.app.modules.system.models import User

            user = db.get(User, ctx.user_id)
            if user is None or user.role != "admin":
                raise ValidationError("无权访问该分组")
        rows = (
            db.query(ArticleGroupItem.article_id)
            .join(Article, Article.id == ArticleGroupItem.article_id)
            .filter(ArticleGroupItem.group_id == group_id, Article.is_deleted.is_(False))
            .order_by(ArticleGroupItem.sort_order.asc())
            .all()
        )
        article_ids = [r[0] for r in rows]
    finally:
        db.close()

    return NodeResult(output={"group_id": group_id, "article_ids": article_ids}, article_ids=[])


register("article_group_source", run_article_group_source)
```
> 先 grep 确认 `ArticleGroup.is_deleted` / `ArticleGroupItem.sort_order` / `Article.is_deleted` 字段名（Task 1 Step 1 已看过）。

- [ ] **Step 2: distribute 节点**

```python
# server/app/modules/pipelines/nodes/distribute_node.py
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError


def run_distribute(ctx: NodeRunContext) -> NodeResult:
    from server.app.modules.system.models import User
    from server.app.modules.tasks.schemas import TaskAccountInput, TaskCreate
    from server.app.modules.tasks.service import create_task

    cfg = ctx.config or {}
    group_id = ctx.inputs.get("group_id") or cfg.get("group_id")
    if not group_id:
        raise ValidationError("distribute 节点缺少 group_id（上游未传且未配置）")
    account_ids = cfg.get("account_ids") or []
    if not account_ids:
        raise ValidationError("distribute 节点需配置至少一个分发账号")
    name = cfg.get("name") or f"自动分发 分组 {group_id}"

    db = ctx.session_factory()
    try:
        user = db.get(User, ctx.user_id)
        role = user.role if user is not None else "operator"
        task_create = TaskCreate(
            name=name,
            task_type="group_round_robin",
            group_id=group_id,
            accounts=[
                TaskAccountInput(account_id=a, sort_order=i) for i, a in enumerate(account_ids)
            ],
            stop_before_publish=False,
        )
        # create_task 内部做审核门禁(_validate_articles_approved)+账号校验，抛命名异常
        task = create_task(db, ctx.user_id, task_create, role=role)
        db.commit()
        task_id = task.id
    finally:
        db.close()

    return NodeResult(output={"task_id": task_id}, article_ids=[])


register("distribute", run_distribute)
```
> 先 grep 确认 `create_task` 签名与 `TaskCreate` 字段（约定已列）。若 `create_task` 的 role 参数为关键字必填/位置，按实际调整。

- [ ] **Step 3: 注册（nodes/__init__.py）**

把两个新模块加入 import（ruff 会按字母排序）：
```python
from server.app.modules.pipelines.nodes import ai_generate_node  # noqa: F401
from server.app.modules.pipelines.nodes import article_group_source  # noqa: F401
from server.app.modules.pipelines.nodes import base  # noqa: F401
from server.app.modules.pipelines.nodes import distribute_node  # noqa: F401
from server.app.modules.pipelines.nodes import input_node  # noqa: F401
```

- [ ] **Step 4: /node-types 增补**

在 `pipelines/router.py:get_node_types` 的 `node_types` 列表追加：
```python
            {"type": "article_group_source", "label": "已审核分组源",
             "config_schema": [
                 {"key": "group_id", "type": "article_group", "label": "内容分组"},
             ]},
            {"type": "distribute", "label": "内容分发",
             "config_schema": [
                 {"key": "account_ids", "type": "accounts", "label": "分发账号"},
                 {"key": "name", "type": "text", "label": "任务名(可空)"},
             ]},
```

- [ ] **Step 5: 验证注册 + 编译 + ruff**

Run:
```
docker exec geo-collab-app-1 sh -lc 'cd /app && python -c "import server.app.modules.pipelines.nodes; from server.app.modules.pipelines.nodes.base import registered_types; print(registered_types())" && ruff check server/app/modules/pipelines/ && ruff format --check server/app/modules/pipelines/'
```
Expected: 打印含 `['ai_generate','article_group_source','distribute','input']`；ruff clean。

- [ ] **Step 6: 提交**

```bash
git add server/app/modules/pipelines/nodes/ server/app/modules/pipelines/router.py
git commit -m "feat(pipelines): article_group_source + distribute nodes + node-types"
```

---

## Task 4: Track B 集成测试（成功 + 门禁）

**Files:**
- Modify: `server/tests/test_pipeline_review_distribute.py`（追加 2 个测试）

- [ ] **Step 1: 追加测试**

需要：一个分组 + 账号夹具。账号创建走现有 API（先 grep 确认创建账号的最简路径：`grep -n "def create_account\|accounts.*post\|@accounts_router.post" server/app/modules/accounts/router.py | head`）。若直接建账号 ORM 更简单，可在测试里用 session_factory 插入 `Account`（参照 `server/tests/test_tasks_api.py` 怎么造账号 + 分组 + approved 文章——**先读它**：`grep -n "Account\|group\|approve\|round_robin" server/tests/test_tasks_api.py | head -40`，复用其夹具写法）。

```python
@pytest.mark.mysql
def test_distribute_node_creates_round_robin_task_for_approved_group(monkeypatch):
    from server.app.modules.tasks.models import PublishTask
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        # 1) 造 approved 文章 + 分组 + 账号（参照 test_tasks_api.py 的夹具写法）
        #    art1, art2 review_status=approved；group g 含两篇；acc1 可用账号
        ...  # 用与 test_tasks_api.py 一致的方式建 account/group/approved articles
        # 2) 建 article_group_source(g) -> distribute(account_ids=[acc1]) pipeline
        snapshot = {"schemaVersion": 1, "nodes": [
            {"node_type": "article_group_source", "name": "源", "node_index": 0,
             "config": {"group_id": g}, "flow_meta": None},
            {"node_type": "distribute", "name": "分发", "node_index": 1,
             "config": {"account_ids": [acc1]},
             "flow_meta": {"inputMapping": [{"from": "group_id", "to": "group_id"}]}},
        ]}
        pid = client.post("/api/pipelines", json={"name": "分发流"}).json()["id"]
        client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snapshot})
        client.post(f"/api/pipelines/{pid}/publish", json={})
        from server.app.modules.pipelines.executor import create_run, run_pipeline
        from server.app.modules.pipelines.models import Pipeline
        with test_app.session_factory() as db:
            p = db.get(Pipeline, pid)
            run = create_run(db, pipeline_id=p.id, user_id=p.user_id)
            db.commit(); run_id = run.id
        run_pipeline(run_id, test_app.session_factory)
        run = client.get(f"/api/pipelines/runs/{run_id}").json()
        assert run["status"] == "done"
        assert run["node_results"]["1"].get("task_id")
        with test_app.session_factory() as db:
            tasks = db.query(PublishTask).all()
            assert any(t.task_type == "group_round_robin" for t in tasks)
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_distribute_node_fails_when_group_has_pending(monkeypatch):
    from server.app.modules.tasks.models import PublishTask
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        # 分组里至少一篇 pending（未过审）
        ...  # 同上夹具，但一篇 article.review_status="pending"
        snapshot = {"schemaVersion": 1, "nodes": [
            {"node_type": "article_group_source", "name": "源", "node_index": 0,
             "config": {"group_id": g}, "flow_meta": None},
            {"node_type": "distribute", "name": "分发", "node_index": 1,
             "config": {"account_ids": [acc1]},
             "flow_meta": {"inputMapping": [{"from": "group_id", "to": "group_id"}]}},
        ]}
        pid = client.post("/api/pipelines", json={"name": "门禁流"}).json()["id"]
        client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snapshot})
        client.post(f"/api/pipelines/{pid}/publish", json={})
        from server.app.modules.pipelines.executor import create_run, run_pipeline
        from server.app.modules.pipelines.models import Pipeline
        with test_app.session_factory() as db:
            p = db.get(Pipeline, pid)
            run = create_run(db, pipeline_id=p.id, user_id=p.user_id)
            db.commit(); run_id = run.id
        run_pipeline(run_id, test_app.session_factory)
        run = client.get(f"/api/pipelines/runs/{run_id}").json()
        assert run["status"] == "failed"
        with test_app.session_factory() as db:
            assert db.query(PublishTask).count() == 0
    finally:
        test_app.cleanup()
```
> `...` 处必须用真实夹具替换（不能留占位）。**先读 `server/tests/test_tasks_api.py`** 复用其建 account / approved 文章 / 分组的写法；approved 文章可直接 `create_article`（默认 review_status=approved）或建后置 approved。distribute 失败时 run=failed（节点抛 ValidationError/ClientError → executor 记 had_failure，无 success → failed）。

- [ ] **Step 2: 运行全文件**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_pipeline_review_distribute.py -q && ruff check server/tests/test_pipeline_review_distribute.py && ruff format --check server/tests/test_pipeline_review_distribute.py'`
Expected: 4 passed（Task1 + Task2 + 本任务 2）+ ruff clean。

- [ ] **Step 3: 提交**

```bash
git add server/tests/test_pipeline_review_distribute.py
git commit -m "test(pipelines): distribute node round-robin success + approval-gate failure"
```

---

## Task 5: 前端属性面板新增 `article_group` / `accounts` 字段类型

**Files:**
- Modify: `web/src/features/pipelines/PipelineEditor.tsx`

- [ ] **Step 1: 读现有 config 渲染 + api**

Run: `grep -n "config_schema\|f.type\|textarea\|number" web/src/features/pipelines/PipelineEditor.tsx | head` 定位 config 字段渲染分支。
Run: `grep -n "listArticleGroups\|ArticleGroup" web/src/api/articles.ts; grep -n "listAccounts\|interface Account\b" web/src/api/accounts.ts web/src/types.ts | head` 确认返回类型字段（组：`id/name`，可能有 review_summary；账号：`id/display_name` 或 `name`）。

- [ ] **Step 2: 加载选项**

在 `PipelineEditor` 顶部加 state + effect：
```tsx
import { listArticleGroups } from "../../api/articles";
import { listAccounts } from "../../api/accounts";
import type { ArticleGroup, Account } from "../../types";
// ...
const [groups, setGroups] = useState<ArticleGroup[]>([]);
const [accounts, setAccounts] = useState<Account[]>([]);
useEffect(() => {
  listArticleGroups().then(setGroups).catch(() => {});
  listAccounts().then(setAccounts).catch(() => {});
}, []);
```
> 若 `ArticleGroup`/`Account` 类型名或字段不同，按 Step 1 实际改（如账号显示名用 `display_name ?? name ?? String(id)`）。

- [ ] **Step 3: 渲染两种字段类型**

在 config 字段渲染的分支里（`f.type === "textarea" ? ... : f.type === "number" ? ... : <text>`）新增两个分支，置于通用 text 之前：
```tsx
{f.type === "article_group" ? (
  <select
    value={String(sel.config[f.key] ?? "")}
    onChange={(e) => updateNode(selected!, {
      config: { ...sel.config, [f.key]: e.target.value ? Number(e.target.value) : undefined },
    })}>
    <option value="">选择分组</option>
    {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
  </select>
) : f.type === "accounts" ? (
  <select
    multiple
    value={((sel.config[f.key] as number[]) ?? []).map(String)}
    onChange={(e) => updateNode(selected!, {
      config: {
        ...sel.config,
        [f.key]: Array.from(e.target.selectedOptions, (o) => Number(o.value)),
      },
    })}>
    {accounts.map((a) => (
      <option key={a.id} value={a.id}>{a.display_name ?? a.name ?? `账号 ${a.id}`}</option>
    ))}
  </select>
) : f.type === "textarea" ? (
  /* 现有 textarea 分支 */
) : ( /* 现有 number/text 分支 */ )}
```
> 把上面两个新分支**接到现有三元链**最前面，保持现有 textarea/number/text 分支不动。`Account` 的显示字段以 Step 1 为准（用存在的那个，避免 TS 报未知属性 —— 若类型上没有 `name` 就只用 `display_name`/`id`）。

- [ ] **Step 4: typecheck + build**

Run（host）：`pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 均通过。

- [ ] **Step 5: 手动冒烟（可选）**

启动前后端，建分发流 pipeline：article_group_source 选一个分组、distribute 多选账号、连线 group_id，保存草稿→发布→运行，run 状态 done 且 node_results 显示 task_id。

- [ ] **Step 6: 提交**

```bash
git add web/src/features/pipelines/PipelineEditor.tsx
git commit -m "feat(pipelines): article_group + accounts config field types in editor"
```

---

## Self-Review 结果

- **Spec 覆盖**：Track A=Task1(helper)+Task2(executor);Track B 节点=Task3,测试=Task2(A)+Task4(B 成功/门禁);前端字段类型=Task5;node-types=Task3 Step4。§10 验收 1=Task2 测试,2=Task4 成功,3=Task4 门禁,4=Task5,5=全程不建表/不碰 scheme。
- **占位符**：Task4 测试夹具用 `...` 占位但**明确要求先读 test_tasks_api.py 复用真实夹具**并替换——执行时不得留 `...`。其余步骤均有完整代码。
- **类型一致**：`mark_pending_and_group(session_factory,*,article_ids,user_id,base_name)->int|None` 在 Task1 定义、Task2 调用一致;节点 `NodeResult(output=...,article_ids=[])`、`NodeRunContext.inputs/config/user_id/session_factory` 与现有 base.py 一致;distribute 用 `TaskCreate(task_type="group_round_robin",group_id,accounts=[TaskAccountInput])` + `create_task(db,user_id,tc,role=...)` 与 PR#19 endpoint 一致;前端 field type 字符串 `article_group`/`accounts` 在 Task3 node-types 与 Task5 渲染一致。
- **待核对点（执行时 grep）**：ArticleGroup/Item 字段名、create_task role 参数形态、Account 显示字段、test_tasks_api 夹具写法、articles/service 既有 logger。
