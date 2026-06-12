# AI 生文拆解为智能体工作流 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 新增三个 pipeline 节点 `question_source → ai_compose → to_review`，把 AI 生文（取问题→生成→进未审核库）拆成可编排、可定时自动运行的智能体工作流，且**端到端真正跑通**。

**Architecture:** 复用 `generate_article_from_prompt`（生成）、`_pick_valid_template`（��板随机+校验）、`mark_pending_and_group`（标 pending+成组）、问题池模型与各端点。执行器在含 `to_review` 节点时跳过现有 Track A 自动成组（显式节点接管）。前端编辑器加 4 种配置字段类型（复用现有 api）。

**Tech Stack:** FastAPI + SQLAlchemy + MySQL + pytest（后端容器跑）；React 19 + Vite + TS（前端 host pnpm）。

---

## 约定（与前序计划一致）

- **唯一改动目标 = geo-collab**；参考项目只读禁改。
- **后端在容器**：`docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest <args>'`。宿主无 python；`server/` bind-mount，host 编辑实时生效。
- **ruff 双门禁**：`ruff check server/` 和 `ruff format --check server/`（用 `ruff format` 修）。`Callable` 从 `collections.abc`。测试 import 放顶部（E402）。
- **前端在 host**：`pnpm --filter @geo/web typecheck` + `build`，硬门禁。
- **错误**：service/node 抛 `ValidationError`/`ClientError`（`server.app.shared.errors`），不抛裸 ValueError。
- **后台线程 session**：节点/生成各自建 session、本线程 commit/close。
- **分支** `feat/ai-generation-nodes`（已基于最新 main，含本 spec）。逐 Task 提交。
- 现有事实（已核实）：
  - `pipelines/nodes/ai_generate_node.py` 是 ai_compose 的范本（含 `generate_article_from_prompt` 顶层 import 便于 monkeypatch、`ThreadPoolExecutor(max_workers=4)`、settings `ai_generate_max_count=20`）。
  - 复用：`from server.app.modules.ai_generation.scheme_executor import _pick_valid_template`（签名 `(db, allowed_ids, user_id, *, rng=None)`，内部用 `get_visible_prompt_template`）；`from server.app.modules.articles.service import mark_pending_and_group`（签名 `(session_factory, *, article_ids, user_id, base_name, fallback_suffix=...)`）。
  - `QuestionItem`（`pool_id`、`category`、`question_text`、`source_active`）、`QuestionPool` 在 `ai_generation/models.py`。
  - 执行器 `executor.py`：`node_specs`（含 `node_type`）在加载节点处构建；Track A 自动成组段是 `if article_ids:`（约 line 168）。
  - 路由 `pipelines/router.py:get_node_types()`（line 59）返回 `{"node_types":[...]}`。
  - 前端 api 已有：`listQuestionPools()`、`listQuestionTypes(poolId)`、`listAiEngines()`（`web/src/api/ai-generation.ts`）；提示词模板列表见 `web/src/api/prompt-templates.ts`（先 grep 确认导出名）。
  - 节点框架 `nodes/base.py`：`NodeRunContext(session_factory,user_id,config,inputs,upstream)`、`NodeResult(output,article_ids)`、`register`、`get_handler`、`registered_types`。

---

## Task 1: `question_source` 节点（问题源）

**Files:**
- Create: `server/app/modules/pipelines/nodes/question_source.py`
- Modify: `server/app/modules/pipelines/nodes/__init__.py`
- Test: `server/tests/test_ai_generation_nodes.py`（新建）

- [ ] **Step 1: 写失败测试（@pytest.mark.mysql）**

```python
# server/tests/test_ai_generation_nodes.py
import pytest

from server.tests.utils import build_test_app


def _make_pool_with_items(app, items):
    """items: list[(category, question_text, source_active)]. 返回 pool_id + user_id。"""
    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool

    with app.session_factory() as db:
        from server.app.modules.system.models import User

        uid = db.query(User).first().id
        pool = QuestionPool(user_id=uid, name="测试池")
        db.add(pool)
        db.flush()
        for i, (cat, text, active) in enumerate(items):
            db.add(QuestionItem(
                pool_id=pool.id, record_id=f"r{i}", fields={},
                category=cat, question_text=text, source_active=active,
            ))
        db.commit()
        return pool.id, uid


@pytest.mark.mysql
def test_question_source_picks_type_and_active(monkeypatch):
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.question_source import run_question_source

    app = build_test_app(monkeypatch)
    try:
        pool_id, uid = _make_pool_with_items(app, [
            ("美食", "怎么做红烧肉", True),
            ("美食", "怎么做糖醋排骨", True),
            ("旅游", "去哪玩", True),
            ("美食", "停用的问题", False),
        ])
        ctx = NodeRunContext(session_factory=app.session_factory, user_id=uid,
                             config={"pool_id": pool_id, "question_type": "美食"},
                             inputs={}, upstream={})
        res = run_question_source(ctx)
        assert "红烧肉" in res.output["question_text"]
        assert "糖醋排骨" in res.output["question_text"]
        assert "去哪玩" not in res.output["question_text"]
        assert "停用" not in res.output["question_text"]
        assert res.output["question_count"] == 2
        # 无匹配类型 → 空 question_text，不报错
        ctx2 = NodeRunContext(session_factory=app.session_factory, user_id=uid,
                              config={"pool_id": pool_id, "question_type": "不存在"},
                              inputs={}, upstream={})
        assert run_question_source(ctx2).output["question_text"] == ""
    finally:
        app.cleanup()
```

- [ ] **Step 2: 运行确认失败**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_ai_generation_nodes.py -q -k question_source'`
Expected: FAIL（ImportError）

- [ ] **Step 3: 实现节点**

```python
# server/app/modules/pipelines/nodes/question_source.py
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError


def run_question_source(ctx: NodeRunContext) -> NodeResult:
    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool
    from server.app.modules.system.models import User

    cfg = ctx.config or {}
    pool_id = cfg.get("pool_id")
    question_type = cfg.get("question_type")
    if not pool_id or not question_type:
        raise ValidationError("question_source 节点需配置 pool_id 与 question_type")

    db = ctx.session_factory()
    try:
        pool = db.get(QuestionPool, pool_id)
        if pool is None or getattr(pool, "is_deleted", False):
            raise ValidationError("问题池不存在")
        if pool.user_id != ctx.user_id:
            user = db.get(User, ctx.user_id)
            if user is None or user.role != "admin":
                raise ValidationError("无权访问该问题池")
        rows = (
            db.query(QuestionItem.question_text)
            .filter(
                QuestionItem.pool_id == pool_id,
                QuestionItem.category == question_type,
                QuestionItem.source_active.is_(True),
            )
            .order_by(QuestionItem.id.asc())
            .all()
        )
        texts = [(r[0] or "").strip() for r in rows if (r[0] or "").strip()]
    finally:
        db.close()

    rendered = "\n".join(f"{i}. {t}" for i, t in enumerate(texts, start=1))
    return NodeResult(
        output={"question_text": rendered, "question_count": len(texts)},
        article_ids=[],
    )


register("question_source", run_question_source)
```
> grep 确认 `QuestionPool` 是否有 `is_deleted` 字段；有则上面 `getattr` 生效，无则去掉该判断。

- [ ] **Step 4: 注册（nodes/__init__.py 增 import）**

在 `nodes/__init__.py` 加 `from server.app.modules.pipelines.nodes import question_source  # noqa: F401`（ruff 会排序，保持与现有 import 风格一致）。

- [ ] **Step 5: 运行通过 + ruff**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_ai_generation_nodes.py -q -k question_source && ruff check server/app/modules/pipelines/nodes/ server/tests/test_ai_generation_nodes.py && ruff format --check server/app/modules/pipelines/nodes/ server/tests/test_ai_generation_nodes.py'`
Expected: PASS + ruff clean。

- [ ] **Step 6: 提交**

```bash
git add server/app/modules/pipelines/nodes/question_source.py server/app/modules/pipelines/nodes/__init__.py server/tests/test_ai_generation_nodes.py
git commit -m "feat(pipelines): question_source node (pull pool questions by type)"
```

---

## Task 2: `ai_compose` 节点（AI创作）

**Files:**
- Create: `server/app/modules/pipelines/nodes/ai_compose.py`
- Modify: `server/app/modules/pipelines/nodes/__init__.py`
- Test: `server/tests/test_ai_generation_nodes.py`（追加）

- [ ] **Step 1: 追加失败测试**

```python
# 追加到 server/tests/test_ai_generation_nodes.py
def _make_gen_template(app, uid, content="写：", enabled=True):
    from server.app.modules.prompt_templates.models import PromptTemplate

    with app.session_factory() as db:
        t = PromptTemplate(name="模板", content=content, scope="generation",
                           user_id=uid, is_enabled=enabled)
        db.add(t)
        db.commit()
        return t.id


@pytest.mark.mysql
def test_ai_compose_generates_with_random_template(monkeypatch):
    calls = {"n": 0}

    def _fake_generate(*, session_factory, user_id, template_content, question_text, model=None):
        from server.app.modules.articles.schemas import ArticleCreate
        from server.app.modules.articles.service import create_article
        import uuid
        calls["n"] += 1
        db = session_factory()
        try:
            art = create_article(db, user_id, ArticleCreate(
                title=f"A{calls['n']}", content_json={"type": "doc", "content": []},
                content_html="<p>x</p>", plain_text="x", word_count=1,
                client_request_id=str(uuid.uuid4())))
            db.commit()
            return art.id
        finally:
            db.close()

    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_compose.generate_article_from_prompt", _fake_generate)
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_compose import run_ai_compose
        from server.app.modules.pipelines.nodes.base import NodeRunContext

        with app.session_factory() as db:
            from server.app.modules.system.models import User
            uid = db.query(User).first().id
        t1 = _make_gen_template(app, uid)
        t2 = _make_gen_template(app, uid)
        ctx = NodeRunContext(session_factory=app.session_factory, user_id=uid,
                             config={"ai_engine": None, "prompt_template_ids": [t1, t2], "count": 3},
                             inputs={"question_text": "1. 怎么做红烧肉"}, upstream={})
        res = run_ai_compose(ctx)
        assert len(res.output["article_ids"]) == 3
        assert res.article_ids == res.output["article_ids"]
        # 空问题 → skipped
        ctx_empty = NodeRunContext(session_factory=app.session_factory, user_id=uid,
                                   config={"prompt_template_ids": [t1], "count": 2},
                                   inputs={"question_text": ""}, upstream={})
        r2 = run_ai_compose(ctx_empty)
        assert r2.output["article_ids"] == [] and r2.output.get("skipped")
        # 模板全无效 → errors 有值、article_ids 空、不抛
        bad = _make_gen_template(app, uid, enabled=False)
        ctx_bad = NodeRunContext(session_factory=app.session_factory, user_id=uid,
                                 config={"prompt_template_ids": [bad], "count": 1},
                                 inputs={"question_text": "1. q"}, upstream={})
        r3 = run_ai_compose(ctx_bad)
        assert r3.output["article_ids"] == [] and r3.output["errors"]
    finally:
        app.cleanup()
```

- [ ] **Step 2: 运行确认失败**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_ai_generation_nodes.py -q -k ai_compose'`
Expected: FAIL（ImportError）

- [ ] **Step 3: 实现节点**（范本 = ai_generate_node.py，差异：允许模板随机 + ai_engine + 空问题跳过）

```python
# server/app/modules/pipelines/nodes/ai_compose.py
from concurrent.futures import ThreadPoolExecutor, as_completed

from server.app.modules.ai_generation.article_writer import generate_article_from_prompt
from server.app.modules.ai_generation.scheme_executor import _pick_valid_template
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError


def run_ai_compose(ctx: NodeRunContext) -> NodeResult:
    cfg = ctx.config or {}
    question_text = ctx.inputs.get("question_text") or cfg.get("question_text") or ""
    if not question_text.strip():
        # 上游无问题（如池暂空）→ 安静跳过，不报错
        return NodeResult(output={"article_ids": [], "errors": [], "skipped": "无问题可生成"},
                          article_ids=[])

    template_ids = cfg.get("prompt_template_ids") or []
    if not template_ids:
        raise ValidationError("ai_compose 节点需配置至少一个提示词模板")
    count = int(cfg.get("count") or 1)
    model = cfg.get("ai_engine")

    from server.app.core.config import get_settings

    max_count = get_settings().ai_generate_max_count
    if count > max_count:
        count = max_count
    if count <= 0:
        raise ValidationError("生成数量需 > 0")

    article_ids: list[int] = []
    errors: list[str] = []

    def _one() -> int:
        # 每篇运行时从允许模板里随机挑一个有效的（每线程自建 session）
        db = ctx.session_factory()
        try:
            tpl = _pick_valid_template(db, template_ids, ctx.user_id)
            if tpl is None:
                raise ValidationError("允许的提示词模板在运行时全部无效")
            template_content = tpl.content
        finally:
            db.close()
        return generate_article_from_prompt(
            session_factory=ctx.session_factory,
            user_id=ctx.user_id,
            template_content=template_content,
            question_text=question_text,
            model=model,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_one) for _ in range(count)]
        for fut in as_completed(futures):
            try:
                article_ids.append(fut.result())
            except Exception as exc:  # 单篇失败不中断，交由 run 聚合 partial_failed
                errors.append(str(exc))

    return NodeResult(output={"article_ids": article_ids, "errors": errors}, article_ids=article_ids)


register("ai_compose", run_ai_compose)
```
> `generate_article_from_prompt` 必须在**模块顶层** import（上方已是），测试 monkeypatch `ai_compose.generate_article_from_prompt` 才能绑定。

- [ ] **Step 4: 注册（nodes/__init__.py）** 加 `from server.app.modules.pipelines.nodes import ai_compose  # noqa: F401`。

- [ ] **Step 5: 运行通过 + ruff**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_ai_generation_nodes.py -q && ruff check server/app/modules/pipelines/nodes/ai_compose.py server/tests/test_ai_generation_nodes.py && ruff format --check server/app/modules/pipelines/nodes/ai_compose.py server/tests/test_ai_generation_nodes.py'`
Expected: PASS + ruff clean。

- [ ] **Step 6: 提交**

```bash
git add server/app/modules/pipelines/nodes/ai_compose.py server/app/modules/pipelines/nodes/__init__.py server/tests/test_ai_generation_nodes.py
git commit -m "feat(pipelines): ai_compose node (random allowed template + ai_engine)"
```

---

## Task 3: `to_review` 节点（进入未审核库）

**Files:**
- Create: `server/app/modules/pipelines/nodes/to_review.py`
- Modify: `server/app/modules/pipelines/nodes/__init__.py`
- Test: `server/tests/test_ai_generation_nodes.py`（追加）

- [ ] **Step 1: 追加失败测试**

```python
@pytest.mark.mysql
def test_to_review_marks_pending_and_groups(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        from server.app.modules.articles.models import Article, ArticleGroupItem
        from server.app.modules.pipelines.nodes.base import NodeRunContext
        from server.app.modules.pipelines.nodes.to_review import run_to_review

        def _mk(title):
            r = client.post("/api/articles", json={
                "title": title, "content_json": {"type": "doc", "content": []},
                "content_html": "<p>x</p>", "plain_text": "x", "word_count": 1, "status": "ready"})
            return r.json()["id"]

        a1, a2 = _mk("甲"), _mk("乙")
        with app.session_factory() as db:
            uid = db.query(Article).first().user_id
        ctx = NodeRunContext(session_factory=app.session_factory, user_id=uid,
                             config={"group_name": "今日生成"},
                             inputs={"article_ids": [a1, a2]}, upstream={})
        res = run_to_review(ctx)
        gid = res.output["group_id"]
        assert gid is not None
        with app.session_factory() as db:
            assert db.get(Article, a1).review_status == "pending"
            assert db.get(Article, a2).review_status == "pending"
            items = db.query(ArticleGroupItem).filter(ArticleGroupItem.group_id == gid).all()
            assert {it.article_id for it in items} == {a1, a2}
        # 空 article_ids → skipped 不建组
        ctx_empty = NodeRunContext(session_factory=app.session_factory, user_id=uid,
                                   config={}, inputs={"article_ids": []}, upstream={})
        assert run_to_review(ctx_empty).output.get("skipped")
    finally:
        app.cleanup()
```

- [ ] **Step 2: 运行确认失败** — `... -k to_review` → FAIL。

- [ ] **Step 3: 实现节点**

```python
# server/app/modules/pipelines/nodes/to_review.py
from server.app.modules.articles.service import mark_pending_and_group
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register


def run_to_review(ctx: NodeRunContext) -> NodeResult:
    cfg = ctx.config or {}
    article_ids = ctx.inputs.get("article_ids") or cfg.get("article_ids") or []
    if not article_ids:
        return NodeResult(output={"skipped": "无文章"}, article_ids=[])

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
> 先 grep `def mark_pending_and_group` 确认参数（含 `fallback_suffix`）；若签名不同按实际调整。`mark_pending_and_group` 顶层 import OK（articles.service 不 import pipelines，无环）。

- [ ] **Step 4: 注册** 加 `from server.app.modules.pipelines.nodes import to_review  # noqa: F401`。

- [ ] **Step 5: 运行通过 + ruff** — 同前命令模式，全 PASS + ruff clean。

- [ ] **Step 6: 提交**

```bash
git add server/app/modules/pipelines/nodes/to_review.py server/app/modules/pipelines/nodes/__init__.py server/tests/test_ai_generation_nodes.py
git commit -m "feat(pipelines): to_review node (mark pending + group into review library)"
```

---

## Task 4: 执行器协调（含 to_review 时跳过自动成组）

**Files:**
- Modify: `server/app/modules/pipelines/executor.py`
- Test: `server/tests/test_ai_generation_nodes.py`（追加）

- [ ] **Step 1: 追加失败测试**（含 to_review → 只一个组）

```python
@pytest.mark.mysql
def test_executor_skips_autogroup_when_to_review_present(monkeypatch):
    def _fake_generate(*, session_factory, user_id, template_content, question_text, model=None):
        from server.app.modules.articles.schemas import ArticleCreate
        from server.app.modules.articles.service import create_article
        import uuid
        db = session_factory()
        try:
            art = create_article(db, user_id, ArticleCreate(
                title="A", content_json={"type": "doc", "content": []},
                content_html="<p>x</p>", plain_text="x", word_count=1,
                client_request_id=str(uuid.uuid4())))
            db.commit(); return art.id
        finally:
            db.close()

    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_compose.generate_article_from_prompt", _fake_generate)
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pool_id, uid = _make_pool_with_items(app, [("美食", "怎么做红烧肉", True)])
        tpl = _make_gen_template(app, uid)
        snap = {"schemaVersion": 1, "nodes": [
            {"node_type": "question_source", "name": "问题源", "node_index": 0,
             "config": {"pool_id": pool_id, "question_type": "美食"}, "flow_meta": None},
            {"node_type": "ai_compose", "name": "创作", "node_index": 1,
             "config": {"prompt_template_ids": [tpl], "count": 2},
             "flow_meta": {"inputMapping": [{"from": "question_text", "to": "question_text"}]}},
            {"node_type": "to_review", "name": "进未审核", "node_index": 2,
             "config": {"group_name": "今日"},
             "flow_meta": {"inputMapping": [{"from": "article_ids", "to": "article_ids"}]}},
        ]}
        pid = client.post("/api/pipelines", json={"name": "生成流"}).json()["id"]
        client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snap})
        client.post(f"/api/pipelines/{pid}/publish", json={})
        from server.app.modules.pipelines.executor import create_run, run_pipeline
        from server.app.modules.pipelines.models import Pipeline
        with app.session_factory() as db:
            p = db.get(Pipeline, pid)
            run = create_run(db, pipeline_id=pid, user_id=p.user_id); db.commit(); rid = run.id
        run_pipeline(rid, app.session_factory)

        run = client.get(f"/api/pipelines/runs/{rid}").json()
        assert run["status"] == "done", run
        with app.session_factory() as db:
            from server.app.modules.articles.models import Article, ArticleGroup, ArticleGroupItem
            arts = run["article_ids"]
            assert len(arts) == 2
            for aid in arts:
                assert db.get(Article, aid).review_status == "pending"
            # 关键：只成一个组（执行器未重复成组）
            group_ids = {it.group_id for it in db.query(ArticleGroupItem)
                         .filter(ArticleGroupItem.article_id.in_(arts)).all()}
            assert len(group_ids) == 1
            assert db.query(ArticleGroup).filter(ArticleGroup.id.in_(group_ids)).count() == 1
    finally:
        app.cleanup()
```

- [ ] **Step 2: 运行确认失败**（当前执行器仍自动成组 → 文章被成两个组 → assert len(group_ids)==1 失败）

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_ai_generation_nodes.py -q -k skips_autogroup'`

- [ ] **Step 3: 改执行器**（先读 `run_pipeline` 确认 node_specs 变量名与 Track A 段）

在构建 `node_specs` 之后、节点循环之前，加：
```python
    has_to_review = any(s["node_type"] == "to_review" for s in node_specs)
```
把 Track A 自动成组段的门条件由 `if article_ids:` 改为：
```python
    # Track A：产出文章 → pending + 成组。含 to_review 节点时由该节点接管，避免重复成组。
    if article_ids and not has_to_review:
```
> `has_to_review` 需在 `node_specs` 作用域可见（它在打开 nodes-session 块内构建；把 `has_to_review` 也在那块算出并带出，或在 `node_specs` 构建后紧接着算——确保它在函数后段 Track A 处可见。实现时把 `has_to_review = ...` 放在 `db.close()` 之前、与 `node_specs` 同作用域，赋值给函数级变量）。

- [ ] **Step 4: 运行通过 + 回归**（含审核分发既有用例确认 Track A 对无 to_review 仍生效）

Run:
```
docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_ai_generation_nodes.py server/tests/test_pipeline_review_distribute.py server/tests/test_pipelines_api.py -q && ruff check server/app/modules/pipelines/executor.py && ruff format --check server/app/modules/pipelines/executor.py'
```
Expected: 全 PASS + ruff clean（既有"生成→自动成组"回归仍绿）。

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/pipelines/executor.py server/tests/test_ai_generation_nodes.py
git commit -m "feat(pipelines): skip auto-grouping when pipeline has a to_review node"
```

---

## Task 5: node-types 注册（3 个节点）

**Files:**
- Modify: `server/app/modules/pipelines/router.py`（`get_node_types`）

- [ ] **Step 1: 增补 node_types 列表**

在 `get_node_types()` 的 `node_types` 列表追加：
```python
            {"type": "question_source", "label": "问题源",
             "config_schema": [
                 {"key": "pool_id", "type": "question_pool", "label": "问题池"},
                 {"key": "question_type", "type": "question_type", "label": "问题类型"},
             ]},
            {"type": "ai_compose", "label": "AI创作",
             "config_schema": [
                 {"key": "ai_engine", "type": "ai_engine", "label": "AI 模型"},
                 {"key": "prompt_template_ids", "type": "prompt_templates", "label": "提示词模板(可多选,运行时随机)"},
                 {"key": "count", "type": "number", "label": "生成数量"},
             ]},
            {"type": "to_review", "label": "进入未审核库",
             "config_schema": [
                 {"key": "group_name", "type": "text", "label": "分组名(可空)"},
             ]},
```

- [ ] **Step 2: 验证 + ruff**

Run:
```
docker exec geo-collab-app-1 sh -lc 'cd /app && python -c "import server.app.modules.pipelines.nodes; from server.app.modules.pipelines.nodes.base import registered_types; print(registered_types())" && ruff check server/app/modules/pipelines/router.py && ruff format --check server/app/modules/pipelines/router.py'
```
Expected: registered_types 含 `ai_compose, question_source, to_review`（+ 既有）；ruff clean。

- [ ] **Step 3: 提交**

```bash
git add server/app/modules/pipelines/router.py
git commit -m "feat(pipelines): node-types for question_source / ai_compose / to_review"
```

---

## Task 6: 前端 — 4 种配置字段类型

**Files:**
- Modify: `web/src/features/pipelines/PipelineEditor.tsx`
- Modify (如需): `web/src/api/ai-generation.ts` / `web/src/api/prompt-templates.ts`（确认导出）

- [ ] **Step 1: 确认 api 导出**

Run: `grep -nE "listQuestionPools|listQuestionTypes|listAiEngines" web/src/api/ai-generation.ts; grep -nE "export (function|const)" web/src/api/prompt-templates.ts | head`
确认 `listQuestionPools()`、`listQuestionTypes(poolId)`、`listAiEngines()` 存在；找到列出 generation 模板的函数（如 `listPromptTemplates`/`listVisiblePrompts`，参数支持 scope=generation）。若无 scope 过滤，前端取全部后按 `scope==="generation" && is_enabled` 过滤。确认 `QuestionPool`/`QuestionType`/`AiEngine`/`PromptTemplate` 类型在 `web/src/types.ts`。

- [ ] **Step 2: PipelineEditor 加载下拉数据**

在 `PipelineEditor` 顶部加 state + effect（与现有 groups/accounts 加载同模式）：
```tsx
import { listAiEngines, listQuestionPools, listQuestionTypes } from "../../api/ai-generation";
// 列模板：用确认到的函数，下同以 listPromptTemplates 占位——实现按实际名替换
import { listPromptTemplates } from "../../api/prompt-templates";
import type { AiEngine, PromptTemplate, QuestionPool } from "../../types";
// state
const [pools, setPools] = useState<QuestionPool[]>([]);
const [engines, setEngines] = useState<AiEngine[]>([]);
const [genTemplates, setGenTemplates] = useState<PromptTemplate[]>([]);
const [typesByPool, setTypesByPool] = useState<Record<number, string[]>>({});
useEffect(() => {
  listQuestionPools().then(setPools).catch(() => {});
  listAiEngines().then(setEngines).catch(() => {});
  listPromptTemplates().then((ts) =>
    setGenTemplates(ts.filter((t) => t.scope === "generation" && t.is_enabled))).catch(() => {});
}, []);
// 按需加载某池的问题类型
const ensureTypes = useCallback((poolId: number) => {
  if (poolId && typesByPool[poolId] === undefined) {
    listQuestionTypes(poolId)
      .then((ts) => setTypesByPool((m) => ({ ...m, [poolId]: ts.map((t) => t.question_type ?? String(t)) })))
      .catch(() => setTypesByPool((m) => ({ ...m, [poolId]: [] })));
  }
}, [typesByPool]);
```
> `QuestionType` 的字段名以 types.ts 实际为准（可能是 `{question_type: string}` 或直接 string）——Step 1 已确认，按实调整 `.map(...)`。

- [ ] **Step 3: 在 config 字段渲染链前面加 4 个分支**

在 `selDef.config_schema.map((f) => ...)` 渲染里，于现有 `article_group`/`accounts`/`textarea`/`number/text` 链之前插入：
```tsx
{f.type === "question_pool" ? (
  <select value={String(sel.config[f.key] ?? "")}
    onChange={(e) => { const v = e.target.value ? Number(e.target.value) : undefined;
      updateNode(selected!, { config: { ...sel.config, [f.key]: v, question_type: "" } });
      if (v) ensureTypes(v); }}>
    <option value="">选择问题池</option>
    {pools.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
  </select>
) : f.type === "question_type" ? (() => {
  const poolId = Number(sel.config["pool_id"]) || 0;
  const opts = typesByPool[poolId] ?? [];
  if (poolId) ensureTypes(poolId);
  return (
    <select value={String(sel.config[f.key] ?? "")} disabled={!poolId}
      onChange={(e) => updateNode(selected!, { config: { ...sel.config, [f.key]: e.target.value } })}>
      <option value="">{poolId ? "选择问题类型" : "请先选问题池"}</option>
      {opts.map((t) => <option key={t} value={t}>{t}</option>)}
    </select>
  );
})() : f.type === "ai_engine" ? (
  <select value={String(sel.config[f.key] ?? "")}
    onChange={(e) => updateNode(selected!, { config: { ...sel.config, [f.key]: e.target.value || null } })}>
    <option value="">系统默认</option>
    {engines.map((en) => <option key={en.id ?? en.model ?? en.label} value={en.model ?? en.id}>{en.label ?? en.model}</option>)}
  </select>
) : f.type === "prompt_templates" ? (
  <select className="peMultiSelect" multiple
    value={((sel.config[f.key] as number[] | undefined) ?? []).map(String)}
    onChange={(e) => updateNode(selected!, { config: { ...sel.config,
      [f.key]: Array.from(e.target.selectedOptions, (o) => Number(o.value)) } })}>
    {genTemplates.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
  </select>
) : /* 现有 article_group 分支... */ }
```
> `AiEngine` 字段（`id`/`model`/`label`）以 types.ts 实际为准——Step 1 确认后用存在的字段，避免 TS 报未知属性。`.peMultiSelect` 样式已在 styles.css（编辑器重样时加的）。

- [ ] **Step 4: typecheck + build**

Run（host）：`pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 通过。

- [ ] **Step 5: 提交**

```bash
git add web/src/features/pipelines/PipelineEditor.tsx web/src/api/
git commit -m "feat(pipelines): editor field types for pool/question-type/ai-engine/templates"
```

---

## Task 7: 端到端集成测试（跑通全链）

**Files:**
- Modify: `server/tests/test_ai_generation_nodes.py`（追加端到端）

- [ ] **Step 1: 追加端到端测试**（问题源→AI创作→进未审核，断言进未审核库）

```python
@pytest.mark.mysql
def test_end_to_end_generate_into_review(monkeypatch):
    def _fake_generate(*, session_factory, user_id, template_content, question_text, model=None):
        from server.app.modules.articles.schemas import ArticleCreate
        from server.app.modules.articles.service import create_article
        import uuid
        # 断言上游问题确实传进来了
        assert "红烧肉" in question_text
        db = session_factory()
        try:
            art = create_article(db, user_id, ArticleCreate(
                title="成品", content_json={"type": "doc", "content": []},
                content_html="<p>x</p>", plain_text="x", word_count=1,
                client_request_id=str(uuid.uuid4())))
            db.commit(); return art.id
        finally:
            db.close()

    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_compose.generate_article_from_prompt", _fake_generate)
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pool_id, uid = _make_pool_with_items(app, [("美食", "怎么做红烧肉", True)])
        tpl = _make_gen_template(app, uid)
        snap = {"schemaVersion": 1, "nodes": [
            {"node_type": "question_source", "name": "问题源", "node_index": 0,
             "config": {"pool_id": pool_id, "question_type": "美食"}, "flow_meta": None},
            {"node_type": "ai_compose", "name": "创作", "node_index": 1,
             "config": {"prompt_template_ids": [tpl], "count": 2},
             "flow_meta": {"inputMapping": [{"from": "question_text", "to": "question_text"}]}},
            {"node_type": "to_review", "name": "进未审核", "node_index": 2,
             "config": {"group_name": "端到端"},
             "flow_meta": {"inputMapping": [{"from": "article_ids", "to": "article_ids"}]}},
        ]}
        pid = client.post("/api/pipelines", json={"name": "AI生文智能体", "type": "generation"}).json()["id"]
        client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snap})
        client.post(f"/api/pipelines/{pid}/publish", json={})
        from server.app.modules.pipelines.executor import create_run, run_pipeline
        from server.app.modules.pipelines.models import Pipeline
        with app.session_factory() as db:
            p = db.get(Pipeline, pid)
            run = create_run(db, pipeline_id=pid, user_id=p.user_id); db.commit(); rid = run.id
        run_pipeline(rid, app.session_factory)

        run = client.get(f"/api/pipelines/runs/{rid}").json()
        assert run["status"] == "done", run
        assert len(run["article_ids"]) == 2
        # 文章出现在"未审核"列表
        listed = client.get("/api/articles?review_status=pending&limit=50").json()
        listed_ids = {a["id"] for a in (listed if isinstance(listed, list) else listed.get("items", []))}
        assert set(run["article_ids"]).issubset(listed_ids)
    finally:
        app.cleanup()
```
> `GET /api/articles?review_status=pending` 的返回结构（list 还是 {items}）先 grep `articles/router.py` 确认，断言相应调整。

- [ ] **Step 2: 运行全文件 + ruff**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_ai_generation_nodes.py -q && ruff check server/tests/test_ai_generation_nodes.py && ruff format --check server/tests/test_ai_generation_nodes.py'`
Expected: 全 PASS（question_source / ai_compose / to_review / 执行器协调 / 端到端）+ ruff clean。

- [ ] **Step 3: 提交**

```bash
git add server/tests/test_ai_generation_nodes.py
git commit -m "test(pipelines): end-to-end question_source -> ai_compose -> to_review"
```

---

## Self-Review 结果

- **Spec 覆盖**：§3.1 question_source=Task1;§3.2 ai_compose=Task2;§3.3 to_review=Task3;§4 执行器协调=Task4;§5.1 node-types=Task5;§5.2 前端字段类型=Task6;§6 测试=各 Task + Task7 端到端;§9 验收 1=Task6,2/5=Task7,3=Task4,4=Task1/2/3 的空集分支,6=全程不改 scheme/不建表。无遗漏。
- **占位符**：无 TBD;每步给完整代码;对未读精确处（QuestionPool.is_deleted、prompt-templates 列表函数名、AiEngine/QuestionType 字段、articles list 返回结构、executor node_specs 变量名）给"先 grep 确认"指令。
- **类型一致**：`run_question_source`/`run_ai_compose`/`run_to_review` 的 `NodeRunContext(session_factory,user_id,config,inputs,upstream)`、`NodeResult(output,article_ids)` 跨 Task 一致;数据流字段 `question_text`(源→创作)、`article_ids`(创作→送审) 一致;`_pick_valid_template(db, template_ids, user_id)`、`mark_pending_and_group(session_factory,*,article_ids,user_id,base_name,fallback_suffix)` 与现有签名一致;node-types 的 type 字符串(question_source/ai_compose/to_review)与节点 register、前端字段类型(question_pool/question_type/ai_engine/prompt_templates)一致。
- **待核对点（执行时 grep）**：QuestionPool.is_deleted、prompt-templates 列表函数 + scope 过滤、AiEngine/QuestionType TS 字段、`/api/articles` 返回结构、executor `has_to_review` 作用域。
