# Pipelines 编排引擎 生产级加固 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把产品同学合并进 main 的「流程编排引擎」(`server/app/modules/pipelines/`，PR #20) 从"玩具"加固到生产级——堵掉绕过审核发布 / 重复发布 / 崩溃卡死 / 删库撞外键四条高危链路，修正状态聚合与越权读，消除与 `ai_generation/scheme_executor` 的重复造轮子。

**Architecture:** 模块沿用 scheme_executor 的"后台线程 + 三段式 run"骨架，本计划**不重写骨架**，只补它抄漏的防护（启动恢复、并发闸、错误可见性、上游失败传播）并复用既有工具（`get_visible_prompt_template`、`mark_pending_and_group`、`recover_stuck_*` 范式）。改动按 P0(阻断生产)→P1(正确性/安全)→P2(技术债) 分三阶段，每阶段独立可上线。

**Tech Stack:** FastAPI · SQLAlchemy 2.0 (Mapped) · Alembic · MySQL 8 (InnoDB) · pytest(mysql marker) · React 19 + TS。

---

## 背景：工程必读约定（来自 CLAUDE.md / 项目记忆）

执行本计划前务必知道这些，否则测试跑不起来、改动会和既有约定冲突：

1. **MySQL only**。测试需要 `GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test`（库名必须含 `test`）。
2. **跑测试的坑**：`conda activate` 在工具 shell 里不一定生效。用项目 env 的 python 全路径跑 pytest，例如
   `<env>/python.exe -m pytest server/tests/xxx.py -q`（CI 用 `mysql:8.0` service）。
3. **测试 schema 来自 models（`Base.metadata.create_all`），不是 alembic**。所以任何 DB 约束改动（外键 ondelete、唯一约束）**必须同时改 `models.py` 和写 alembic 迁移**，两边对齐——否则测试库与生产库行为不一致（参见 scheme_executor 里关于 `uq_article_groups_user_name` 测试库有/生产库 drop 的注释）。
4. **service 层只抛命名异常**（`ClientError`/`ConflictError`/`ValidationError`/`AccountError`），全局 handler 映射成 4xx；裸 `ValueError` 没有兜底会变 500。`ConflictError → 409`。
5. **DB session 非线程安全**：后台/worker 线程内必须自建 session 并在本线程 commit+close，绝不跨线程传 session 或 ORM 对象。本模块现状这块是对的，不要破坏。
6. **新建文章默认 `review_status='approved'`**（`create_article`），AI 内容必须经审核——`mark_pending_and_group` 的职责就是把 approved 翻成 pending 强制送审。这条不变量是 P0 的核心。
7. 迁移头部当前是 `0038`（`down_revision="0037"`），新迁移接在它后面。
8. 每个 Task 末尾 `git commit`，小步提交。提交信息按仓库习惯（`feat(pipelines):` / `fix(pipelines):`）。

## 涉及文件总览（按职责）

| 文件 | 改动职责 |
|------|----------|
| `server/app/modules/pipelines/executor.py` | run 状态聚合修正、error_message、上游失败传播、成组失败可见化、并发闸 |
| `server/app/modules/pipelines/router.py` | 后台线程 None 守卫 + 异常兜底 |
| `server/app/modules/pipelines/service.py` | publish_draft 行锁串行化 version_no |
| `server/app/modules/pipelines/models.py` | 外键 ondelete=CASCADE、version_no 唯一约束 |
| `server/app/modules/pipelines/recovery.py` (新建) | `recover_stuck_pipeline_runs` 启动恢复 |
| `server/app/modules/pipelines/nodes/ai_generate_node.py` | 复用 `get_visible_prompt_template`（修越权读）、并发生文 |
| `server/app/modules/articles/service.py` | `mark_pending_and_group` 加 `fallback_suffix` 参数 |
| `server/app/modules/ai_generation/scheme_executor.py` | `_group_run_articles` 改调 `mark_pending_and_group`（去重） |
| `server/app/main.py` | 启动时调 `recover_stuck_pipeline_runs` |
| `server/alembic/versions/0039_pipelines_fk_cascade.py` (新建) | 外键 CASCADE + version 唯一索引迁移 |
| `web/src/features/pipelines/PipelineEditor.tsx` | 轮询健壮性 |
| `server/tests/test_pipeline_*.py` | 各 Task 的回归测试 |

---

# PHASE 0 — 阻断生产项（必须先全部完成）

## Task 1：启动时恢复卡死的 pipeline run

**问题**：`run_pipeline` 置 `running` 后进内存执行，进程被杀/重启 → run 永久卡 `running`，前端无限轮询。`create_app()` 的启动恢复只复位 publish 记录，完全不碰 `pipeline_runs`。

**设计**：参照 `tasks.recover_stuck_records` 范式。启动时进程刚起、没有任何 run 实际在跑，所以**所有** `status in ('running','pending')` 的 run 都是上次崩溃的残留 → 直接复位成 `failed`。无需租约/阈值。

**Files:**
- Create: `server/app/modules/pipelines/recovery.py`
- Modify: `server/app/main.py`（启动恢复段，约 line 107-121）
- Test: `server/tests/test_pipeline_recovery.py`

- [ ] **Step 1: 写失败测试**

`server/tests/test_pipeline_recovery.py`：

```python
import pytest

from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_recover_stuck_pipeline_runs_resets_running_and_pending(monkeypatch):
    from server.app.modules.pipelines.models import Pipeline, PipelineRun
    from server.app.modules.pipelines.recovery import recover_stuck_pipeline_runs

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            uid = (
                __import__("server.app.modules.system.models", fromlist=["User"]).User
            )
            user_id = db.query(uid).first().id
            p = Pipeline(user_id=user_id, name="p", has_draft=False)
            db.add(p)
            db.flush()
            db.add(PipelineRun(pipeline_id=p.id, user_id=user_id, status="running",
                               node_results={}, article_ids=[]))
            db.add(PipelineRun(pipeline_id=p.id, user_id=user_id, status="pending",
                               node_results={}, article_ids=[]))
            db.add(PipelineRun(pipeline_id=p.id, user_id=user_id, status="done",
                               node_results={}, article_ids=[]))
            db.commit()

        with test_app.session_factory() as db:
            recover_stuck_pipeline_runs(db)

        with test_app.session_factory() as db:
            rows = db.query(PipelineRun).order_by(PipelineRun.id.asc()).all()
            assert rows[0].status == "failed" and rows[0].error_message
            assert rows[1].status == "failed" and rows[1].error_message
            assert rows[2].status == "done"  # 已终态的不动
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_pipeline_recovery.py -q`
Expected: FAIL — `ModuleNotFoundError: server.app.modules.pipelines.recovery`

- [ ] **Step 3: 实现 recovery.py**

`server/app/modules/pipelines/recovery.py`：

```python
# server/app/modules/pipelines/recovery.py
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.app.core.time import utcnow
from server.app.modules.pipelines.models import PipelineRun

logger = logging.getLogger(__name__)


def recover_stuck_pipeline_runs(db: Session) -> None:
    """启动时复位上次崩溃残留的 pipeline run。

    进程刚启动时没有任何 run 真正在执行，因此所有 running/pending 都是僵死残留，
    直接置 failed（无租约机制，故不按阈值，全量复位）。
    """
    now = utcnow()
    runs = list(
        db.execute(
            select(PipelineRun).where(PipelineRun.status.in_(("running", "pending")))
        )
        .scalars()
        .all()
    )
    for run in runs:
        run.status = "failed"
        run.error_message = "进程重启：运行在上次执行中意外中断"
        run.completed_at = now
    if runs:
        logger.warning(
            "Recovered %d stuck pipeline runs: %s", len(runs), [r.id for r in runs]
        )
        db.commit()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_pipeline_recovery.py -q`
Expected: PASS

- [ ] **Step 5: 接入 main.py 启动恢复**

在 `server/app/main.py` 现有的启动恢复块里（`recover_stuck_records(recover_db)` 之后、同一个 try 内）追加：

```python
    try:
        recover_db = SessionLocal()
        try:
            recover_stuck_records(recover_db)
            from server.app.modules.pipelines.recovery import recover_stuck_pipeline_runs

            recover_stuck_pipeline_runs(recover_db)
        finally:
            recover_db.close()
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).exception(
            "Startup recovery failed — stuck records may not have been reset"
        )
```

- [ ] **Step 6: 跑全量 pipeline 测试 + 确认 app 能启动**

Run: `python -m pytest server/tests/test_pipeline_recovery.py server/tests/test_pipelines_api.py -q`
Expected: PASS（启动恢复在 `build_test_app→create_app()` 里会跑一次，空库下是 no-op）

- [ ] **Step 7: Commit**

```bash
git add server/app/modules/pipelines/recovery.py server/app/main.py server/tests/test_pipeline_recovery.py
git commit -m "fix(pipelines): 启动时复位崩溃残留的 running/pending run，避免永久卡死"
```

---

## Task 2：同一 pipeline 单活跃 run 闸（并发 / 幂等）

**问题**：`create_run` 零并发防护，双击/重试/多标签页 → N 个 run 并行 → `distribute` 把同一批文章发 N 遍。

**设计**：在 `executor.create_run` 里先对 pipeline 行 `SELECT ... FOR UPDATE` 串行化，再检查是否已有 `pending/running` run，有则抛 `ConflictError`（→409）。`create_run` 同时被 route 和测试直接调用，把闸放这里两条路径都覆盖。

**Files:**
- Modify: `server/app/modules/pipelines/executor.py:17-27`（`create_run`）
- Test: `server/tests/test_pipelines_api.py`（追加）

- [ ] **Step 1: 写失败测试**

在 `server/tests/test_pipelines_api.py` 末尾追加：

```python
@pytest.mark.mysql
def test_create_run_rejects_when_active_run_exists(monkeypatch):
    import pytest as _pytest

    from server.app.modules.pipelines.executor import create_run
    from server.app.modules.pipelines.models import Pipeline, PipelineRun
    from server.app.modules.system.models import User
    from server.app.shared.errors import ConflictError

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            user_id = db.query(User).first().id
            p = Pipeline(user_id=user_id, name="p", has_draft=False)
            db.add(p)
            db.flush()
            db.add(PipelineRun(pipeline_id=p.id, user_id=user_id, status="running",
                               node_results={}, article_ids=[]))
            db.commit()
            pid = p.id

        with test_app.session_factory() as db:
            with _pytest.raises(ConflictError):
                create_run(db, pipeline_id=pid, user_id=user_id)
    finally:
        test_app.cleanup()
```

> 注：`test_pipelines_api.py` 顶部已有 `import pytest` 与 `from server.tests.utils import build_test_app`；若无则补上。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_pipelines_api.py::test_create_run_rejects_when_active_run_exists -q`
Expected: FAIL — `DID NOT RAISE ConflictError`（当前 create_run 无脑建 run）

- [ ] **Step 3: 给 create_run 加闸**

`server/app/modules/pipelines/executor.py`，改 `create_run`（顶部 import 补 `Pipeline` 与 `ConflictError`）：

```python
from server.app.modules.pipelines.models import Pipeline, PipelineNode, PipelineRun
from server.app.shared.errors import ConflictError
```

```python
def create_run(db, *, pipeline_id: int, user_id: int) -> PipelineRun:
    # 串行化同一 pipeline 的 run 创建：锁住 pipeline 行后检查活跃 run，避免并发重复运行
    db.query(Pipeline).filter(Pipeline.id == pipeline_id).with_for_update().first()
    active = (
        db.query(PipelineRun.id)
        .filter(
            PipelineRun.pipeline_id == pipeline_id,
            PipelineRun.status.in_(("pending", "running")),
        )
        .first()
    )
    if active is not None:
        raise ConflictError("该工作流已有正在运行的任务，请等待其完成后再运行")
    run = PipelineRun(
        pipeline_id=pipeline_id,
        user_id=user_id,
        status="pending",
        node_results={},
        article_ids=[],
    )
    db.add(run)
    db.flush()
    return run
```

- [ ] **Step 4: 跑测试确认通过 + 既有测试不回归**

Run: `python -m pytest server/tests/test_pipelines_api.py server/tests/test_pipeline_review_distribute.py -q`
Expected: PASS（既有测试每个 run 跑完即终态，不会触发闸）

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/pipelines/executor.py server/tests/test_pipelines_api.py
git commit -m "fix(pipelines): 单活跃 run 闸（FOR UPDATE + 活跃检查），堵重复发布"
```

---

## Task 3：成组 / 送审失败不再静默（审核不变量）

**问题**：run 终态先写 `done`，之后才 best-effort 调 `mark_pending_and_group`，且该函数内部又吞掉一切返回 None。成组失败时 run 仍显示 done，但 AI 文章保持 `approved` 且未成组 → 可被绕过审核发布。

**设计**：捕获 `mark_pending_and_group` 返回值；产出了文章但成组返回 None（失败）时，把 run 从 done 降级为 `partial_failed` 并写 `error_message`，让前端/运营看得见。

**Files:**
- Modify: `server/app/modules/pipelines/executor.py:123-146`（成组块）
- Test: `server/tests/test_pipeline_review_distribute.py`（追加）

- [ ] **Step 1: 写失败测试**

在 `server/tests/test_pipeline_review_distribute.py` 末尾追加：

```python
@pytest.mark.mysql
def test_run_downgraded_when_grouping_fails(monkeypatch):
    from server.app.modules.articles.schemas import ArticleCreate
    from server.app.modules.articles.service import create_article

    def _fake_generate(*, session_factory, user_id, template_content, question_text, model=None):
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
    # 模拟成组失败：helper 返回 None
    monkeypatch.setattr(
        "server.app.modules.pipelines.executor.mark_pending_and_group",
        lambda *a, **k: None, raising=False)

    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        tpl = _make_generation_template(client)
        pid = client.post("/api/pipelines", json={"name": "生成流"}).json()["id"]
        snapshot = {"schemaVersion": 1, "nodes": [
            {"node_type": "input", "name": "源", "node_index": 0,
             "config": {"question_text": "主题"}, "flow_meta": None},
            {"node_type": "ai_generate", "name": "生文", "node_index": 1,
             "config": {"prompt_template_id": tpl, "count": 1},
             "flow_meta": {"inputMapping": [{"from": "question_text", "to": "question_text"}]}},
        ]}
        client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snapshot})
        client.post(f"/api/pipelines/{pid}/publish", json={})

        from server.app.modules.pipelines.executor import create_run, run_pipeline
        from server.app.modules.pipelines.models import Pipeline
        with test_app.session_factory() as db:
            p = db.get(Pipeline, pid)
            run = create_run(db, pipeline_id=p.id, user_id=p.user_id)
            db.commit()
            run_id = run.id
        run_pipeline(run_id, test_app.session_factory)

        run = client.get(f"/api/pipelines/runs/{run_id}").json()
        assert run["status"] == "partial_failed", run
        assert run["error_message"] and "成组" in run["error_message"]
    finally:
        test_app.cleanup()
```

> 注：`mark_pending_and_group` 在 `executor.py` 是函数内 import 的（`from server.app.modules.articles.service import mark_pending_and_group`）。Step 3 改成模块顶部 import，才能让上面这个 monkeypatch（patch executor 模块属性）生效。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_pipeline_review_distribute.py::test_run_downgraded_when_grouping_fails -q`
Expected: FAIL — `assert 'done' == 'partial_failed'`

- [ ] **Step 3: 改成组块捕获返回值并降级**

`server/app/modules/pipelines/executor.py`：把 `mark_pending_and_group` 的 import 提到文件顶部（与其它 import 同级），删掉成组块内的函数内 import；并改写 `# Track A` 整块为：

```python
    # Track A: 产出文章 → pending + 成组。失败不能静默——会让未审文章被误用
    if article_ids:
        gid = None
        try:
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
                gid = mark_pending_and_group(
                    session_factory,
                    article_ids=article_ids,
                    user_id=uid,
                    base_name=base_name,
                    fallback_suffix=f"#{run_id}",  # Task 7 加该参数；先写好调用
                )
        except Exception:  # noqa: BLE001
            logger.exception("pipeline run %s post-grouping failed", run_id)

        if gid is None:
            # 成组/送审失败：降级 run 状态 + 写明原因，避免 UI 显示成功
            db = session_factory()
            try:
                run = db.get(PipelineRun, run_id)
                if run is not None:
                    if run.status == "done":
                        run.status = "partial_failed"
                    note = "文章已生成但送审/成组失败，请手动核对审核状态"
                    run.error_message = (
                        f"{run.error_message}; {note}" if run.error_message else note
                    )
                    db.commit()
            finally:
                db.close()
```

顶部 import 区加：

```python
from server.app.modules.articles.service import mark_pending_and_group
```

> 若 Task 7 尚未做，`fallback_suffix=` 这个 kwarg 会 `TypeError`。**Task 3 与 Task 7 需一起合入**（顺序：先 Task 7 给 helper 加参数，再 Task 3 用它，或本计划按 Task 编号顺序执行时把 Task 7 提到 Task 3 前）。执行时若先做 Task 3，临时去掉 `fallback_suffix=` 那一行，Task 7 再补。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_pipeline_review_distribute.py -q`
Expected: PASS（含既有 4 个用例 + 新降级用例）

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/pipelines/executor.py server/tests/test_pipeline_review_distribute.py
git commit -m "fix(pipelines): 成组/送审失败时降级 run 状态+写明原因，堵绕过审核路径"
```

---

## Task 4：迁移 0039 — pipeline 子表外键 CASCADE + version_no 唯一（models 同步）

**问题**：0038 四张表外键全缺 `ondelete`（默认 RESTRICT）；删 pipeline 仅靠应用层手删兜过，SQL 直删/删 user 会撞 1451（PR #18 同款坑）。`_next_version_no` 用 `max+1` 且 `(pipeline_id, version_no)` 仅普通索引，并发可重号。

**设计**：(a) 子表→pipelines 的外键加 `ON DELETE CASCADE`；user 外键保持 RESTRICT（与全库一致，用户为软删，不硬删）。(b) `(pipeline_id, version_no)` 改唯一索引。**models 与迁移同步改**（测试走 models）。

**Files:**
- Create: `server/alembic/versions/0039_pipelines_fk_cascade.py`
- Modify: `server/app/modules/pipelines/models.py`
- Test: `server/tests/test_pipelines_api.py`（追加唯一约束 + 级联删除断言）

- [ ] **Step 1: 确认 user 外键约定（只读核实，不改）**

Run: `grep -rn "ForeignKey(\"users.id\"" server/app/modules/articles/models.py server/app/modules/tasks/models.py`
Expected: 观察既有表对 `users.id` 是否带 `ondelete`。若全不带（软删约定）→ 本 Task 维持 user 外键 RESTRICT；若带某策略 → 镜像之。**下面按"维持 RESTRICT"编写**（最常见）。

- [ ] **Step 2: 写失败测试**

在 `server/tests/test_pipelines_api.py` 末尾追加：

```python
@pytest.mark.mysql
def test_version_no_unique_and_cascade_delete(monkeypatch):
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    from server.app.modules.pipelines.models import (
        Pipeline, PipelineNode, PipelineRun, PipelineVersion,
    )
    from server.app.modules.system.models import User

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            uid = db.query(User).first().id
            p = Pipeline(user_id=uid, name="p", has_draft=False)
            db.add(p)
            db.flush()
            db.add(PipelineVersion(pipeline_id=p.id, version_no=1, snapshot={}, created_by=uid))
            db.commit()
            pid = p.id

        # (a) 同 (pipeline_id, version_no) 第二条应撞唯一约束
        with test_app.session_factory() as db:
            db.add(PipelineVersion(pipeline_id=pid, version_no=1, snapshot={}, created_by=uid))
            with _pytest.raises(IntegrityError):
                db.commit()

        # (b) 删 pipeline 应级联删子表（DB 层 CASCADE，不靠应用手删）
        with test_app.session_factory() as db:
            uid2 = db.query(User).first().id
            db.add(PipelineNode(pipeline_id=pid, node_type="input", name="n",
                                node_index=0, config={}))
            db.add(PipelineRun(pipeline_id=pid, user_id=uid2, status="done",
                               node_results={}, article_ids=[]))
            db.commit()
            db.execute(__import__("sqlalchemy").text(
                "DELETE FROM pipelines WHERE id = :i"), {"i": pid})
            db.commit()
            assert db.query(PipelineNode).filter_by(pipeline_id=pid).count() == 0
            assert db.query(PipelineRun).filter_by(pipeline_id=pid).count() == 0
            assert db.query(PipelineVersion).filter_by(pipeline_id=pid).count() == 0
    finally:
        test_app.cleanup()
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python -m pytest server/tests/test_pipelines_api.py::test_version_no_unique_and_cascade_delete -q`
Expected: FAIL（无唯一约束 → 第二条 insert 不报错；无 CASCADE → 裸 SQL DELETE 撞外键 1451）

- [ ] **Step 4: 改 models.py（测试库 schema 来源）**

`server/app/modules/pipelines/models.py`：

子表外键加 `ondelete="CASCADE"`（三处 `pipeline_id`）：

```python
# PipelineNode
    pipeline_id: Mapped[int] = mapped_column(
        ForeignKey("pipelines.id", ondelete="CASCADE"), index=True
    )
# PipelineVersion
    pipeline_id: Mapped[int] = mapped_column(
        ForeignKey("pipelines.id", ondelete="CASCADE"), index=True
    )
# PipelineRun
    pipeline_id: Mapped[int] = mapped_column(
        ForeignKey("pipelines.id", ondelete="CASCADE"), index=True
    )
```

`PipelineVersion` 加唯一约束（顶部 import 补 `UniqueConstraint`）：

```python
from sqlalchemy import (
    JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text,
    UniqueConstraint,
)
```

```python
class PipelineVersion(Base):
    __tablename__ = "pipeline_versions"
    __table_args__ = (
        UniqueConstraint("pipeline_id", "version_no", name="uq_pipeline_versions_pipeline_version"),
    )
    ...
```

- [ ] **Step 5: 写迁移 0039**

`server/alembic/versions/0039_pipelines_fk_cascade.py`：

```python
"""pipelines: child FK ON DELETE CASCADE + unique (pipeline_id, version_no)

Revision ID: 0039
Revises: 0038
Create Date: 2026-06-05
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHILDREN = ["pipeline_nodes", "pipeline_versions", "pipeline_runs"]


def _drop_fk_to(table: str, ref_table: str) -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t "
            "AND REFERENCED_TABLE_NAME = :r"
        ),
        {"t": table, "r": ref_table},
    ).fetchall()
    for (name,) in rows:
        op.drop_constraint(name, table, type_="foreignkey")


def upgrade() -> None:
    # 1) 子表 → pipelines 外键改 CASCADE（先删旧匿名 FK，再建命名 FK）
    for child in _CHILDREN:
        _drop_fk_to(child, "pipelines")
        op.create_foreign_key(
            f"fk_{child}_pipeline", child, "pipelines",
            ["pipeline_id"], ["id"], ondelete="CASCADE",
        )
    # 2) (pipeline_id, version_no) 普通索引 → 唯一
    op.drop_index("ix_pipeline_versions_pipeline_version", table_name="pipeline_versions")
    op.create_index(
        "uq_pipeline_versions_pipeline_version",
        "pipeline_versions", ["pipeline_id", "version_no"], unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_pipeline_versions_pipeline_version", table_name="pipeline_versions")
    op.create_index(
        "ix_pipeline_versions_pipeline_version",
        "pipeline_versions", ["pipeline_id", "version_no"],
    )
    for child in _CHILDREN:
        op.drop_constraint(f"fk_{child}_pipeline", child, type_="foreignkey")
        op.create_foreign_key(None, child, "pipelines", ["pipeline_id"], ["id"])
```

- [ ] **Step 6: 跑测试确认通过 + 迁移可应用**

Run: `python -m pytest server/tests/test_pipelines_api.py::test_version_no_unique_and_cascade_delete -q`
Expected: PASS

验证迁移链无多头（仿 `test_fts_and_migrations.py` 的范式，或）：
Run: `python -m alembic heads`
Expected: 单一 head `0039`

- [ ] **Step 7: Commit**

```bash
git add server/alembic/versions/0039_pipelines_fk_cascade.py server/app/modules/pipelines/models.py server/tests/test_pipelines_api.py
git commit -m "fix(pipelines): 子表外键 ON DELETE CASCADE + version_no 唯一约束（models+迁移同步）"
```

---

# PHASE 1 — 正确性 / 安全

## Task 5：状态聚合修正（input 不计成功）+ 写 error_message

**问题**：`had_success` 把 input 节点当成功 → `input → ai_generate 全失败` 被判 `partial_failed` 而非 `failed`。且 `error_message` 字段全链路定义却从不写入，失败原因只埋在 node_results。

**设计**：`had_success` 只在真正产出业务结果时置位（`ai_generate` 产出 article_ids，或 `distribute` 产出 task_id）；input / 读取类节点不计。聚合后把各节点错误汇总写入 `error_message`（仿 `scheme_executor._aggregate_run`）。

**Files:**
- Modify: `server/app/modules/pipelines/executor.py:93-121`
- Test: `server/tests/test_pipeline_logic.py` 或 `test_pipeline_review_distribute.py`（追加）

- [ ] **Step 1: 写失败测试**

在 `server/tests/test_pipeline_review_distribute.py` 末尾追加（验证全失败 → failed + error_message）：

```python
@pytest.mark.mysql
def test_run_all_generation_failed_is_failed_with_error_message(monkeypatch):
    def _boom(*, session_factory, user_id, template_content, question_text, model=None):
        raise RuntimeError("LLM 503")

    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_generate_node.generate_article_from_prompt",
        _boom)

    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        tpl = _make_generation_template(client)
        pid = client.post("/api/pipelines", json={"name": "全失败流"}).json()["id"]
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
            db.commit()
            run_id = run.id
        run_pipeline(run_id, test_app.session_factory)

        run = client.get(f"/api/pipelines/runs/{run_id}").json()
        assert run["status"] == "failed", run          # 之前会错判 partial_failed
        assert run["error_message"] and "LLM 503" in run["error_message"]
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_pipeline_review_distribute.py::test_run_all_generation_failed_is_failed_with_error_message -q`
Expected: FAIL — `assert 'partial_failed' == 'failed'`

- [ ] **Step 3: 改聚合逻辑 + 写 error_message**

`server/app/modules/pipelines/executor.py`，循环内把：

```python
            # ai_generate 节点内单篇失败也算部分失败
            if result.output.get("errors"):
                had_failure = True
            if result.article_ids or spec["node_type"] == "input":
                had_success = True
```

替换为：

```python
            # ai_generate 节点内单篇失败也算部分失败
            if result.output.get("errors"):
                had_failure = True
            # had_success 仅在真正产出业务结果时置位：
            # ai_generate 产文(result.article_ids) 或 distribute 建任务(output.task_id)。
            # input / 读取类节点(article_group_source) 不计入成功，避免零产出被误判 partial。
            if result.article_ids or result.output.get("task_id"):
                had_success = True
```

聚合状态块前，构造 error_message；改最终写回块：

```python
    # 聚合状态
    if had_failure and had_success:
        status = "partial_failed"
    elif had_failure:
        status = "failed"
    else:
        status = "done"

    # 汇总各节点错误，写入 run.error_message（失败原因不止埋在 node_results）
    error_parts: list[str] = []
    for k, v in node_results.items():
        if isinstance(v, dict):
            if v.get("error"):
                error_parts.append(f"node#{k}: {v['error']}")
            elif v.get("errors"):
                error_parts.append(f"node#{k}: {'; '.join(str(e) for e in v['errors'])}")
    error_message = "; ".join(error_parts)[:2000] or None

    db = session_factory()
    try:
        run = db.get(PipelineRun, run_id)
        if run is not None:
            run.status = status
            run.node_results = node_results
            run.article_ids = article_ids
            run.error_message = error_message
            run.completed_at = utcnow()
            db.commit()
    finally:
        db.close()
```

- [ ] **Step 4: 跑测试确认通过 + 不回归**

Run: `python -m pytest server/tests/test_pipeline_review_distribute.py server/tests/test_pipeline_logic.py -q`
Expected: PASS（既有"门禁失败→failed"、"成功→done" 用例仍过；新全失败用例过）

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/pipelines/executor.py server/tests/test_pipeline_review_distribute.py
git commit -m "fix(pipelines): 状态聚合不把 input 当成功（零产出判 failed）+ 写 error_message"
```

---

## Task 6：ai_generate 复用 `get_visible_prompt_template`（修越权读 + 去重）

**问题**：`ai_generate_node` 用**不带可见性过滤**的 `get_prompt_template` + 手搓四项校验，丢了 `user_id` 维度可见性 → 任意用户能引用任意模板（越权读）。`scheme_executor` 已有 `get_visible_prompt_template(db, tid, user_id=, scope=)`。

**设计**：换用 `get_visible_prompt_template`，删手搓校验，对齐 scheme 行为。

**Files:**
- Modify: `server/app/modules/pipelines/nodes/ai_generate_node.py:1-25`
- Test: `server/tests/test_pipeline_logic.py`（追加）

- [ ] **Step 1: 写失败测试**

在 `server/tests/test_pipeline_logic.py` 末尾追加（另一用户的模板不可用）：

```python
import pytest

from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_ai_generate_rejects_other_users_template(monkeypatch):
    from server.app.modules.pipelines.nodes.ai_generate_node import run_ai_generate
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.prompt_templates.models import PromptTemplate
    from server.app.modules.system.models import User
    from server.app.shared.errors import ValidationError

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            owner = db.query(User).first()
            other = User(username="other", role="operator", is_active=True,
                         must_change_password=False)
            other.set_password("x")
            db.add(other)
            db.flush()
            tpl = PromptTemplate(user_id=owner.id, name="私有", content="写：",
                                 scope="generation", is_enabled=True)
            db.add(tpl)
            db.commit()
            tpl_id, other_id = tpl.id, other.id

        ctx = NodeRunContext(
            session_factory=test_app.session_factory, user_id=other_id,
            config={"prompt_template_id": tpl_id, "count": 1},
            inputs={"question_text": "主题"}, upstream={})
        with pytest.raises(ValidationError):
            run_ai_generate(ctx)
    finally:
        test_app.cleanup()
```

> 若 `PromptTemplate` 的字段名与此不符，先 `grep -n "class PromptTemplate" -A 20 server/app/modules/prompt_templates/models.py` 核对后微调夹具。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_pipeline_logic.py::test_ai_generate_rejects_other_users_template -q`
Expected: FAIL — 当前用 `get_prompt_template`（无 user 过滤），不抛 ValidationError 而是去生文

- [ ] **Step 3: 换用 get_visible_prompt_template**

`server/app/modules/pipelines/nodes/ai_generate_node.py`，改 import 与模板校验段：

```python
from server.app.modules.ai_generation.article_writer import generate_article_from_prompt
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.modules.prompt_templates.service import get_visible_prompt_template
from server.app.shared.errors import ValidationError
```

```python
    db = ctx.session_factory()
    try:
        tpl = get_visible_prompt_template(
            db, template_id, user_id=ctx.user_id, scope="generation"
        )
        if tpl is None or not tpl.is_enabled:
            raise ValidationError("提示词模板无效（不存在/无权访问/停用/删除/非 generation）")
        template_content = tpl.content
    finally:
        db.close()
```

- [ ] **Step 4: 跑测试确认通过 + 既有生文用例不回归**

Run: `python -m pytest server/tests/test_pipeline_logic.py server/tests/test_pipeline_review_distribute.py -q`
Expected: PASS（owner 自己的模板仍可用 → 既有 `test_pipeline_run_marks...` 过；他人模板被拒）

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/pipelines/nodes/ai_generate_node.py server/tests/test_pipeline_logic.py
git commit -m "fix(pipelines): ai_generate 复用 get_visible_prompt_template，修越权读模板+去重"
```

---

## Task 7：统一成组逻辑（scheme 复用 helper）+ 兜底后缀用 run_id

**问题**：`mark_pending_and_group` 与 `scheme_executor._group_run_articles` 整段复制（作者建了参数化 helper 却没回收 scheme 那份）；且 pipeline 兜底后缀用不唯一的 `#article_ids[0]`（scheme 原版用唯一的 `#run.id`，被劣化）。

**设计**：给 `mark_pending_and_group` 加 `fallback_suffix: str` 参数用于 IntegrityError 兜底命名；让 `scheme_executor._group_run_articles` 改调它（消除 80 行复制）。

**Files:**
- Modify: `server/app/modules/articles/service.py`（`mark_pending_and_group`）
- Modify: `server/app/modules/ai_generation/scheme_executor.py`（`_group_run_articles`）
- Test: `server/tests/test_pipeline_review_distribute.py`（已有 `test_mark_pending_and_group_*`，追加兜底用例）

- [ ] **Step 1: 写失败测试**

在 `server/tests/test_pipeline_review_distribute.py` 末尾追加（同 base_name 第二次成组应带稳定后缀、不再撞约束）：

```python
@pytest.mark.mysql
def test_mark_pending_and_group_fallback_suffix_is_stable(monkeypatch):
    from server.app.modules.articles.models import ArticleGroup
    from server.app.modules.articles.service import mark_pending_and_group
    from server.app.modules.articles.models import Article

    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        a1 = _make_article(client, "甲")
        a2 = _make_article(client, "乙")
        with test_app.session_factory() as db:
            uid = db.query(Article).first().user_id
        # 先占用 base_name
        gid1 = mark_pending_and_group(test_app.session_factory, article_ids=[a1],
                                      user_id=uid, base_name="撞名组", fallback_suffix="#101")
        # 再用同 base_name → 应落到带 fallback_suffix 的名字，不报错
        gid2 = mark_pending_and_group(test_app.session_factory, article_ids=[a2],
                                      user_id=uid, base_name="撞名组", fallback_suffix="#202")
        assert gid1 is not None and gid2 is not None and gid1 != gid2
        with test_app.session_factory() as db:
            names = {g.name for g in db.query(ArticleGroup).all()}
            assert "撞名组" in names and "撞名组 #202" in names
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_pipeline_review_distribute.py::test_mark_pending_and_group_fallback_suffix_is_stable -q`
Expected: FAIL — `TypeError: mark_pending_and_group() got an unexpected keyword argument 'fallback_suffix'`

- [ ] **Step 3: 给 helper 加 fallback_suffix 参数**

`server/app/modules/articles/service.py`，改 `mark_pending_and_group` 签名与两处命名（`#{article_ids[0]}` → `fallback_suffix`）：

```python
def mark_pending_and_group(
    session_factory, *, article_ids: list[int], user_id: int, base_name: str,
    fallback_suffix: str | None = None,
) -> int | None:
    """把文章标 review_status='pending' 并归入新 ArticleGroup（名 base_name）。
    撞 (user_id, name) 唯一约束时改用 base_name + fallback_suffix（应传调用方稳定唯一值，
    如 run_id）。best-effort：失败记日志、不抛。返回 group_id 或 None。"""
    if not article_ids:
        return None
    suffix = fallback_suffix or f"#{article_ids[0]}"
    try:
        from sqlalchemy.exc import IntegrityError

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
            name = f"{base_name} {suffix}" if exists is not None else base_name
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
                group = ArticleGroup(user_id=user_id, name=f"{base_name} {suffix}")
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
        _logger.exception(
            "mark_pending_and_group failed (user=%s, n=%s)", user_id, len(article_ids)
        )
        return None
```

- [ ] **Step 4: scheme_executor 改调 helper（去重）**

`server/app/modules/ai_generation/scheme_executor.py`，把整个 `_group_run_articles`（约 line 245-327）替换为薄封装：

```python
def _group_run_articles(run_id: int, session_factory: SessionFactory) -> None:
    """方案运行产出文章：标 pending + 归入新方案组。复用 articles.mark_pending_and_group。"""
    from server.app.modules.articles.service import mark_pending_and_group

    db = session_factory()
    try:
        run = db.get(GenerationSchemeRun, run_id)
        if run is None:
            return
        article_ids = list(run.article_ids or [])
        if not article_ids:
            return
        scheme = db.get(GenerationScheme, run.scheme_id)
        scheme_name = scheme.name if scheme is not None else f"方案 {run.scheme_id}"
        base_name = f"{run.created_at:%Y/%m/%d %H:%M} · {scheme_name}"
        uid = run.user_id
        rid = run.id
    finally:
        db.close()

    mark_pending_and_group(
        session_factory, article_ids=article_ids, user_id=uid,
        base_name=base_name, fallback_suffix=f"#{rid}",
    )
```

- [ ] **Step 5: 跑测试确认通过 + scheme 不回归**

Run: `python -m pytest server/tests/test_pipeline_review_distribute.py -q -k "mark_pending or group"`
Expected: PASS

Run: `python -m pytest server/tests/ -q -k scheme`
Expected: PASS（scheme 成组行为不变——名字 + 后缀语义一致）

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/articles/service.py server/app/modules/ai_generation/scheme_executor.py server/tests/test_pipeline_review_distribute.py
git commit -m "refactor(pipelines): scheme 复用 mark_pending_and_group 去重 + 兜底后缀改稳定 run_id"
```

> 完成本 Task 后，回到 Task 3 Step 3 确认 `fallback_suffix=f\"#{run_id}\"` 那行已生效（若 Task 3 先做时临时删过，现在补回并重跑 Task 3 的测试）。

---

## Task 8：上游失败传播（dependsOnIndex 阻断下游副作用）

**问题**：某节点抛异常后，executor 继续无脑跑下游；下游拿空 upstream，`distribute` 可能静默回退到 config 的 group_id，对**错误分组**建发布任务（副作用已落地，run 却显示 failed）。

**设计**：维护 `failed_indices` 集合；若节点的 `flow_meta.dependsOnIndex` 指向一个已失败的节点，则**不执行**该节点，记错误并计 failure，本节点也加入 failed_indices（链式阻断）。

**Files:**
- Modify: `server/app/modules/pipelines/executor.py`（节点循环）
- Test: `server/tests/test_pipeline_review_distribute.py`（追加）

- [ ] **Step 1: 写失败测试**

在 `server/tests/test_pipeline_review_distribute.py` 末尾追加（上游 source 失败 → 下游 distribute 被阻断、不建任务）：

```python
@pytest.mark.mysql
def test_downstream_blocked_when_dependency_failed(monkeypatch):
    from server.app.modules.tasks.models import PublishTask

    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        acc1 = _create_account(client, test_app.data_dir, "account-a", "Account A")
        # source 指向不存在的 group → 抛错；distribute 自带 config.group_id 兜底（错误分组）
        snapshot = {"schemaVersion": 1, "nodes": [
            {"node_type": "article_group_source", "name": "源", "node_index": 0,
             "config": {"group_id": 999999}, "flow_meta": None},
            {"node_type": "distribute", "name": "分发", "node_index": 1,
             "config": {"account_ids": [acc1], "group_id": 999999},
             "flow_meta": {"dependsOnIndex": 0,
                           "inputMapping": [{"from": "group_id", "to": "group_id"}]}},
        ]}
        pid = client.post("/api/pipelines", json={"name": "依赖流"}).json()["id"]
        client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snapshot})
        client.post(f"/api/pipelines/{pid}/publish", json={})

        from server.app.modules.pipelines.executor import create_run, run_pipeline
        from server.app.modules.pipelines.models import Pipeline
        with test_app.session_factory() as db:
            p = db.get(Pipeline, pid)
            run = create_run(db, pipeline_id=p.id, user_id=p.user_id)
            db.commit()
            run_id = run.id
        run_pipeline(run_id, test_app.session_factory)

        run = client.get(f"/api/pipelines/runs/{run_id}").json()
        assert run["status"] == "failed", run
        assert "上游" in (run["node_results"].get("1", {}).get("error", ""))
        with test_app.session_factory() as db:
            assert db.query(PublishTask).count() == 0  # 下游被阻断，未建错误任务
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_pipeline_review_distribute.py::test_downstream_blocked_when_dependency_failed -q`
Expected: FAIL — 当前下游会拿 config.group_id 兜底建任务，`PublishTask.count() == 1`

- [ ] **Step 3: 实现 failed_indices 阻断**

`server/app/modules/pipelines/executor.py`，节点循环改造：

循环前初始化集合：

```python
    context: dict[int, dict] = {}  # node_index -> output
    node_results: dict[str, Any] = {}
    article_ids: list[int] = []
    had_success = False
    had_failure = False
    failed_indices: set[int] = set()
```

循环体开头（取 upstream 之后、`should_skip` 之前）插入依赖阻断：

```python
        # 上游依赖失败 → 阻断本节点，避免拿空 upstream 静默回退 config 产生副作用
        dep = meta.get("dependsOnIndex") if meta else None
        if dep is not None and dep in failed_indices:
            node_results[str(idx)] = {"error": f"上游节点 #{dep} 失败，已中止本节点"}
            had_failure = True
            failed_indices.add(idx)
            continue
```

异常分支补登记：

```python
        except Exception as exc:
            logger.exception("pipeline run %s node #%s failed", run_id, idx)
            node_results[str(idx)] = {"error": str(exc)}
            had_failure = True
            failed_indices.add(idx)
```

- [ ] **Step 4: 跑测试确认通过 + 不回归**

Run: `python -m pytest server/tests/test_pipeline_review_distribute.py -q`
Expected: PASS（无 dependsOnIndex 的既有用例行为不变；新依赖用例阻断生效）

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/pipelines/executor.py server/tests/test_pipeline_review_distribute.py
git commit -m "fix(pipelines): dependsOnIndex 上游失败时阻断下游，防止 distribute 对错误分组建任务"
```

---

## Task 9：后台线程 None 守卫 + 异常兜底

**问题**：`router.create_run` 直接 `threading.Thread(target=run_pipeline, args=(run_id, factory))`，未检查 `bg_session_factory is None`、线程 target 无 try 包裹（scheme_router 两者都有）。factory 异常时 run 卡 `pending` 且无日志。

**设计**：route 内检查 factory；为 None 时用请求 db 直接把 run 标 failed 并返回 503。线程 target 包一层 try/except 记日志。

**Files:**
- Modify: `server/app/modules/pipelines/router.py:215-230`
- Test: `server/tests/test_pipelines_api.py`（追加）

- [ ] **Step 1: 写失败测试**

在 `server/tests/test_pipelines_api.py` 末尾追加：

```python
@pytest.mark.mysql
def test_create_run_when_factory_missing_fails_run_not_pending(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        pid = client.post("/api/pipelines", json={"name": "无 factory 流"}).json()["id"]
        snapshot = {"schemaVersion": 1, "nodes": [
            {"node_type": "input", "name": "源", "node_index": 0,
             "config": {"question_text": "x"}, "flow_meta": None}]}
        client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snapshot})
        client.post(f"/api/pipelines/{pid}/publish", json={})

        monkeypatch.setattr("server.app.modules.pipelines.router.bg_session_factory", None)
        resp = client.post(f"/api/pipelines/{pid}/runs")
        assert resp.status_code == 503, resp.text
        run_id = resp.json().get("run_id")
        if run_id:
            run = client.get(f"/api/pipelines/runs/{run_id}").json()
            assert run["status"] == "failed"  # 不能卡 pending
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_pipelines_api.py::test_create_run_when_factory_missing_fails_run_not_pending -q`
Expected: FAIL — 当前不检查 None，返回 202 且 run 卡 pending（`run_pipeline(run_id, None)` 在线程里 TypeError）

- [ ] **Step 3: 加守卫 + 线程兜底**

`server/app/modules/pipelines/router.py`，改 `create_run` 路由尾部：

```python
    run = _create_run(db, pipeline_id=p.id, user_id=user.id)
    db.commit()
    run_id = run.id

    factory = bg_session_factory
    if factory is None:
        import logging

        logging.getLogger(__name__).error(
            "bg_session_factory 未注入，run %s 无法执行", run_id
        )
        from server.app.modules.pipelines.models import PipelineRun

        run_obj = db.get(PipelineRun, run_id)
        if run_obj is not None:
            run_obj.status = "failed"
            run_obj.error_message = "后台执行器未就绪（bg_session_factory 未注入）"
            db.commit()
        return JSONResponse(
            status_code=503, content={"run_id": run_id, "status": "failed"}
        )

    def _runner() -> None:
        try:
            run_pipeline(run_id, factory)
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "pipeline run %s thread crashed", run_id
            )

    threading.Thread(target=_runner, daemon=True).start()
    return JSONResponse(status_code=202, content={"run_id": run_id, "status": "pending"})
```

- [ ] **Step 4: 跑测试确认通过 + 正常运行不回归**

Run: `python -m pytest server/tests/test_pipelines_api.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/pipelines/router.py server/tests/test_pipelines_api.py
git commit -m "fix(pipelines): create_run 守卫 bg_session_factory=None + 线程异常兜底（对齐 scheme_router）"
```

---

## Task 10：前端轮询健壮性

**问题**：`getRun` 抛错时轮询不停（无 try/catch），并发 `onRun` 时旧 interval 句柄泄漏。

**设计**：轮询回调 try/catch，累计失败 N 次停轮询；`onRun` 入口先清旧 interval（已有，确认即可）。

**Files:**
- Modify: `web/src/features/pipelines/PipelineEditor.tsx:100-115`

- [ ] **Step 1: 改 onRun 轮询**

`web/src/features/pipelines/PipelineEditor.tsx`，把 `onRun` 的 `setInterval` 回调改为带错误处理：

```tsx
  const onRun = async () => {
    try {
      const { run_id } = await startRun(pipelineId);
      setRunStatus("running");
      if (pollRef.current != null) { clearInterval(pollRef.current); pollRef.current = null; }
      let failures = 0;
      pollRef.current = setInterval(async () => {
        try {
          const r = await getRun(run_id);
          failures = 0;
          setRunStatus(`${r.status}（文章 ${r.article_ids.length} 篇）`);
          if (["done", "failed", "partial_failed"].includes(r.status)) {
            if (pollRef.current != null) { clearInterval(pollRef.current); pollRef.current = null; }
          }
        } catch {
          failures += 1;
          if (failures >= 5 && pollRef.current != null) {
            clearInterval(pollRef.current); pollRef.current = null;
            setRunStatus("运行状态获取失败，请刷新");
          }
        }
      }, 1500);
    } catch (e) {
      toast(e instanceof Error ? e.message : "运行失败", "error");
    }
  };
```

- [ ] **Step 2: typecheck**

Run: `pnpm --filter @geo/web typecheck`
Expected: PASS（无类型错误）

- [ ] **Step 3: Commit**

```bash
git add web/src/features/pipelines/PipelineEditor.tsx
git commit -m "fix(pipelines): 前端轮询失败不再无限重试，累计失败停轮询并提示"
```

---

# PHASE 2 — 技术债 / 优化（可选，按需排期）

## Task 11：publish_draft 行锁串行化 version_no

**问题**：`_next_version_no` 用 `max+1` check-then-insert，并发 publish 同 pipeline 可重号（Task 4 已加唯一约束兜底，但会撞 IntegrityError 报错）。

**设计**：`publish_draft` 开头对 pipeline 行 `with_for_update`，串行化同 pipeline 的并发发布。

**Files:**
- Modify: `server/app/modules/pipelines/service.py:78`（`publish_draft` 开头）

- [ ] **Step 1: 加行锁**

`server/app/modules/pipelines/service.py`，`publish_draft` 函数体第一行（`if not p.has_draft...` 之前）加：

```python
def publish_draft(db: Session, p: Pipeline, *, remark: str | None, user_id: int) -> int:
    # 串行化同一 pipeline 的并发发布，避免 version_no 重号
    db.query(Pipeline).filter(Pipeline.id == p.id).with_for_update().first()
    if not p.has_draft or not p.draft_snapshot:
        raise ClientError("没有可发布的草稿")
    ...
```

- [ ] **Step 2: 跑既有 publish 测试确认不回归**

Run: `python -m pytest server/tests/test_pipelines_api.py -q -k "publish or version"`
Expected: PASS（顺序发布 v1、v2 行为不变）

- [ ] **Step 3: Commit**

```bash
git add server/app/modules/pipelines/service.py
git commit -m "fix(pipelines): publish_draft 行锁串行化 version_no 计算"
```

---

## Task 12：ai_generate 节点内并发生文（ThreadPoolExecutor）

**问题**：节点内 `count` 篇串行生文（scheme 用 `ThreadPoolExecutor(max_workers=4)`），长流程放大崩溃窗口。

**设计**：用线程池并发，每个 future 自带 session（`generate_article_from_prompt` 已自管 session），future 收集结果保证线程安全。

**Files:**
- Modify: `server/app/modules/pipelines/nodes/ai_generate_node.py:29-47`
- Test: 既有 `test_pipeline_run_marks_articles_pending_and_groups`（count=2）即覆盖；保证产 2 篇。

- [ ] **Step 1: 改为并发**

`server/app/modules/pipelines/nodes/ai_generate_node.py`，把串行 for 循环替换为：

```python
    from concurrent.futures import ThreadPoolExecutor, as_completed

    article_ids: list[int] = []
    errors: list[str] = []

    def _one() -> int:
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
            except Exception as exc:  # 单篇失败不中断，交由 run 聚合
                errors.append(str(exc))

    return NodeResult(
        output={"article_ids": article_ids, "errors": errors},
        article_ids=article_ids,
    )
```

- [ ] **Step 2: 跑测试确认不回归**

Run: `python -m pytest server/tests/test_pipeline_review_distribute.py -q -k "marks_articles or generation_failed"`
Expected: PASS（count=2 仍产 2 篇；全失败仍 failed）

- [ ] **Step 3: Commit**

```bash
git add server/app/modules/pipelines/nodes/ai_generate_node.py
git commit -m "perf(pipelines): ai_generate 节点内并发生文（max_workers=4），对齐 scheme"
```

---

## Task 13：scheme_run 启动恢复（对称补齐）

**问题**：`GenerationSchemeRun` 与 pipeline_run 同病——崩溃后卡 running 无 recover（Task 1 只修了 pipeline）。

**设计**：在 `scheme_executor` 加 `recover_stuck_scheme_runs(db)`，main.py 启动恢复块一并调用。逻辑同 Task 1。

**Files:**
- Modify: `server/app/modules/ai_generation/scheme_executor.py`（追加函数）
- Modify: `server/app/main.py`（启动恢复块追加一行）
- Test: `server/tests/`（仿 `test_pipeline_recovery.py` 加 scheme 版）

- [ ] **Step 1: 写测试 + 实现 + 接入 + commit**

实现（追加到 scheme_executor.py）：

```python
def recover_stuck_scheme_runs(db) -> None:
    """启动时复位崩溃残留的方案运行（running/pending → failed）。"""
    from sqlalchemy import select

    runs = list(
        db.execute(
            select(GenerationSchemeRun).where(
                GenerationSchemeRun.status.in_(("running", "pending"))
            )
        ).scalars().all()
    )
    for run in runs:
        run.status = "failed"
        run.error_message = "进程重启：运行在上次执行中意外中断"
        run.completed_at = utcnow()
    if runs:
        logger.warning("Recovered %d stuck scheme runs", len(runs))
        db.commit()
```

main.py 启动恢复块追加（在 `recover_stuck_pipeline_runs(recover_db)` 之后）：

```python
            from server.app.modules.ai_generation.scheme_executor import (
                recover_stuck_scheme_runs,
            )

            recover_stuck_scheme_runs(recover_db)
```

测试仿 `test_pipeline_recovery.py`（造 running scheme run → 调函数 → 断言 failed）。

Run: `python -m pytest server/tests/ -q -k "recover"`
Expected: PASS

```bash
git add server/app/modules/ai_generation/scheme_executor.py server/app/main.py server/tests/
git commit -m "fix(ai-gen): scheme run 启动恢复，对称补齐崩溃残留复位"
```

---

## Task 14：死代码评估（决策项，非强制改）

**问题**：审查发现疑似未用面：`NodeRunContext.upstream`（所有节点只读 `inputs`）、两个零调用端点 `GET /versions/{id}` 与 `GET /{pipeline_id}/runs`。

**设计**：这些是**预留接口性质**，不盲删。本 Task 是决策 + 落注释，而非删代码：

- [ ] **Step 1: 给预留面加注释，避免后续维护者误判**

`server/app/modules/pipelines/nodes/base.py` 的 `NodeRunContext.upstream` 字段加注释：

```python
    upstream: dict  # 预留：节点可直接读全量上游输出；当前内置节点只用 inputs
```

`server/app/modules/pipelines/router.py` 两个端点上方各加一行注释：

```python
# 预留给「版本详情/diff」UI（当前前端未调用，勿当死代码删）
@router.get("/versions/{version_id}")
...
# 预留给「运行历史」列表 UI（当前前端只轮询单个 run，勿当死代码删）
@router.get("/{pipeline_id}/runs")
```

- [ ] **Step 2: Commit**

```bash
git add server/app/modules/pipelines/nodes/base.py server/app/modules/pipelines/router.py
git commit -m "docs(pipelines): 标注预留接口（upstream/版本详情/运行历史端点），防误删"
```

> 若产品确认这些 UI 永不做，改为删除端点 + `upstream` 字段 + `VersionRead.snapshot`，另起一个 cleanup commit。

---

## 收尾：全量验证

全部 Task 完成后跑一遍硬门禁（CI 同口径）：

- [ ] 后端 lint/format/类型/测试：
```bash
ruff check server/
ruff format --check server/
mypy server/app
python -m pytest server/tests/test_pipeline_logic.py server/tests/test_pipelines_api.py server/tests/test_pipeline_review_distribute.py server/tests/test_pipeline_recovery.py -q
```
- [ ] 前端：
```bash
pnpm --filter @geo/web typecheck
pnpm --filter @geo/web build
```
- [ ] 迁移单头：`python -m alembic heads` → 仅 `0039`

---

## Self-Review 记录（计划作者自查）

- **Spec 覆盖**：4 个 agent 的 TOP 风险逐条映射 → Task 1(卡死) / Task 2(并发重发) / Task 3(绕审核) / Task 4(外键) / Task 5(状态错判+error_message) / Task 6(越权读模板) / Task 7(成组重复+后缀) / Task 8(上游失败传播) / Task 9(None 守卫) / Task 10(前端轮询) / Task 11(version_no) / Task 12(串行生文) / Task 13(scheme 对称) / Task 14(死代码决策)。无遗漏 TOP 项。
- **跨 Task 类型一致**：`mark_pending_and_group(..., fallback_suffix=)` 在 Task 3 调用、Task 7 定义——已在 Task 3 标注依赖顺序。`recover_stuck_pipeline_runs` / `recover_stuck_scheme_runs` 命名对齐 `recover_stuck_records`。`had_success` 判据（article_ids / task_id）与节点实际 output 字段一致（已核对 distribute 输出 `task_id`、ai_generate 输出 `article_ids` + NodeResult.article_ids）。
- **无占位符**：每个 code step 均为完整可粘贴代码；唯一外部依赖（PromptTemplate 字段名、user 外键约定）已用 grep 核实步骤兜住。
- **测试库 vs 生产库**：Task 4 同时改 models + 迁移，符合"测试走 create_all、生产走 alembic"约定。
