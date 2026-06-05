# Pipeline 编排引擎 / 智能体调度 —— Vibe-coding 整改实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 PR #20/#21/#22（pipeline 编排 + 智能体调度）三份代码审查（Claude/Codex/DeepSeek 去重去假阳性后）确认的生产级缺陷：审核绕过、调度漏跑、前端并发竞态、多进程恢复误杀、无并发上限、迁移兼容。

**Architecture:** 后端 FastAPI + SQLAlchemy/Alembic（MySQL only），生文/编排跑在 API 进程后台线程；前端 React 19 + TS（无单测运行器）。整改以**最小根因修复**为主：审核门禁前移到生文落库点、调度改"最近到点 slot + claim 去重"、节点执行加顶层兜底、迁移补回填。不引入新框架。

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy 2 / Alembic / pytest（MySQL，需 `GEO_TEST_DATABASE_URL`）；React 19 / Vite / TypeScript strict（`pnpm --filter @geo/web typecheck`）。

---

## 范围说明与执行顺序

本计划跨多个子系统（生文/审核、调度、执行器、迁移、前端、并发恢复）。按**触发条件分阶段**，每阶段独立可测、可单独合并：

| 阶段 | 主题 | 阻塞级别 |
|---|---|---|
| Phase 0 | 止血：审核绕过 + 前端竞态 + 节点崩溃兜底 | 合并前必做 |
| Phase 1 | 调度可靠性：漏跑 + claim 事务 | 启用调度前必做 |
| Phase 2 | 迁移/数据兼容：tags 回填 + PATCH 清空 | 跨版本部署前必做 |
| Phase 3 | 韧性/并发：恢复 leader + 全局闸 + count 上限 + 快照冻结 + 索引 | 扩容前必做 |
| Phase 4 | 治理/边界：跨租户分发决策 + 跨午夜窗 | 排期 |
| Phase 5 | 技术债/打磨：去重 + 临时代码 + 小性能 + 一致性 + 补测试 | 排期 |

**通用约定**
- 后端测试：`set GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test` 后 `python -m pytest <path> -q`。纯逻辑测试（schedule_calc/flow_meta）无需 DB；带 `build_test_app`/`@pytest.mark.mysql` 的需 DB。本机用 env python 全路径跑（conda activate 在工具 shell 里不生效）。
- 前端无单测运行器：前端任务以 `pnpm --filter @geo/web typecheck` + 文档化手动复现步骤验收。
- 每个任务结束 commit；commit message 结尾加 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。
- 数据库异常一律抛命名异常（`ClientError`/`ConflictError`/`ValidationError`），不抛裸 `ValueError`。

---

# Phase 0 — 止血

## Task 1: AI 生文一律"未审"落库（堵审核绕过根因）

**根因**：`Article.review_status` 默认 `approved`，AI 文章靠 run 后 best-effort `mark_pending_and_group` 才翻 `pending`；删除竞态或翻转失败时留 `approved`，可经 `task_type="single"` 直接发布。修法：在唯一的 AI 生文落库点把文章生而 `pending`，与后置成组解耦。

**Files:**
- Modify: `server/app/modules/ai_generation/article_writer.py`（`generate_article_from_prompt`，create_article 之后、commit 之前）
- Test: `server/tests/test_article_writer_review_status.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
# server/tests/test_article_writer_review_status.py
import pytest

from server.tests.utils import build_test_app  # 提供 DB/会话/admin


def _fake_completion(text: str):
    class _Msg:
        content = text

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    return _Resp()


@pytest.mark.mysql
def test_generated_article_is_born_pending(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        monkeypatch.setattr(
            "litellm.completion", lambda **kw: _fake_completion("# 标题\n\n正文段落。")
        )
        from server.app.db.session import SessionLocal
        from server.app.modules.ai_generation.article_writer import (
            generate_article_from_prompt,
        )
        from server.app.modules.articles.models import Article

        article_id = generate_article_from_prompt(
            session_factory=SessionLocal,
            user_id=app.admin_user_id,
            template_content="写一篇文章：{{question}}",
            question_text="测试问题",
            model=None,
        )
        db = SessionLocal()
        try:
            art = db.get(Article, article_id)
            assert art is not None
            assert art.review_status == "pending"  # 不再是 approved
        finally:
            db.close()
    finally:
        app.cleanup()
```

> 注：`build_test_app` 返回对象的 admin 字段名以 `server/tests/utils.py` 实际为准（多为 `admin_user_id` 或 `.admin.id`）；执行时若字段名不同，按该文件调整本测试同一处引用。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_article_writer_review_status.py -q`
Expected: FAIL，断言 `art.review_status == "pending"` 不成立（当前为 `approved`）。

- [ ] **Step 3: 最小实现**

`article_writer.py` 中 create_article 之后、commit 之前加一行：

```python
    db = session_factory()
    try:
        article = create_article(db, user_id, article_payload)
        # AI 生文一律未审：不依赖 run 后 mark_pending_and_group 翻转
        # （防删除竞态 / best-effort 失败导致 approved 文章被直接发布）
        article.review_status = "pending"
        db.commit()
        return article.id
    except Exception:
        db.rollback()
        raise
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_article_writer_review_status.py -q`
Expected: PASS。

- [ ] **Step 5: 回归 —— 既有方案/编排成组测试仍绿**

Run: `python -m pytest server/tests/test_scheme_runs.py server/tests/test_pipeline_grouping.py -q`
Expected: PASS（成组逻辑不变，pending 翻转变幂等）。

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/ai_generation/article_writer.py server/tests/test_article_writer_review_status.py
git commit -m "fix(ai-gen): AI 生文一律 review_status=pending 落库，堵审核绕过根因"
```

---

## Task 2: 删除 pipeline 时拒绝活跃 run（防孤儿 + 数据一致）

**Files:**
- Modify: `server/app/modules/pipelines/service.py`（`delete_pipeline`）
- Test: `server/tests/test_pipelines_api.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.mysql
def test_delete_pipeline_rejected_when_active_run(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.db.session import SessionLocal
        from server.app.modules.pipelines import service as svc
        from server.app.modules.pipelines.models import PipelineRun
        from server.app.shared.errors import ConflictError

        db = SessionLocal()
        try:
            p = svc.create_pipeline(db, user_id=app.admin_user_id, name="t", description=None)
            db.add(PipelineRun(pipeline_id=p.id, user_id=app.admin_user_id,
                               status="running", node_results={}, article_ids=[]))
            db.commit()
            with pytest.raises(ConflictError):
                svc.delete_pipeline(db, p)
        finally:
            db.close()
    finally:
        app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_pipelines_api.py::test_delete_pipeline_rejected_when_active_run -q`
Expected: FAIL（当前直接删除、不抛）。

- [ ] **Step 3: 实现守卫**

`delete_pipeline` 开头加活跃 run 检查：

```python
def delete_pipeline(db: Session, p: Pipeline) -> None:
    active = (
        db.query(PipelineRun.id)
        .filter(PipelineRun.pipeline_id == p.id, PipelineRun.status.in_(("pending", "running")))
        .first()
    )
    if active is not None:
        raise ConflictError("该工作流有正在运行的任务，请等待其完成后再删除")
    db.query(PipelineNode).filter(PipelineNode.pipeline_id == p.id).delete()
    db.query(PipelineVersion).filter(PipelineVersion.pipeline_id == p.id).delete()
    db.query(PipelineRun).filter(PipelineRun.pipeline_id == p.id).delete()
    db.delete(p)
    db.flush()
```

并在文件顶部 `from server.app.shared.errors import ClientError, ValidationError` 改为也 import `ConflictError`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_pipelines_api.py::test_delete_pipeline_rejected_when_active_run -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/pipelines/service.py server/tests/test_pipelines_api.py
git commit -m "fix(pipelines): 删除工作流前拒绝活跃 run（409），防孤儿文章/竞态"
```

---

## Task 3: run_pipeline 顶层兜底 + flow_meta 防御，杜绝卡 `running`

**根因**：`should_skip`/`apply_input_mapping` 在 per-node `try` 外，且 run_pipeline 无顶层兜底；畸形 flow_meta 让线程崩、run 永久 `running`。

**Files:**
- Modify: `server/app/modules/pipelines/executor.py`（`run_pipeline` 节点循环 + 顶层兜底）
- Modify: `server/app/modules/pipelines/flow_meta.py`（`should_skip`/`apply_input_mapping` 容错非 dict）
- Test: `server/tests/test_pipeline_executor_hardening.py`（追加）

- [ ] **Step 1: 写失败测试（畸形 flow_meta 不应让 run 卡 running）**

```python
@pytest.mark.mysql
def test_malformed_flow_meta_marks_run_failed_not_stuck(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.db.session import SessionLocal
        from server.app.modules.pipelines.executor import run_pipeline
        from server.app.modules.pipelines.models import Pipeline, PipelineNode, PipelineRun

        db = SessionLocal()
        try:
            p = Pipeline(user_id=app.admin_user_id, name="t", has_draft=False)
            db.add(p); db.flush()
            # condition 不是 dict —— 旧代码会在 should_skip 抛 AttributeError
            db.add(PipelineNode(pipeline_id=p.id, node_type="input", name="in",
                                node_index=0, config={"question_text": "x"},
                                flow_meta={"condition": "not-a-dict"}))
            run = PipelineRun(pipeline_id=p.id, user_id=app.admin_user_id,
                              status="pending", node_results={}, article_ids=[])
            db.add(run); db.commit()
            run_id = run.id
        finally:
            db.close()

        run_pipeline(run_id, SessionLocal)  # 不应抛

        db = SessionLocal()
        try:
            run = db.get(PipelineRun, run_id)
            assert run.status in ("failed", "partial_failed", "done")  # 关键：不是 running/pending
            assert run.completed_at is not None
        finally:
            db.close()
    finally:
        app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_pipeline_executor_hardening.py::test_malformed_flow_meta_marks_run_failed_not_stuck -q`
Expected: FAIL（异常逃逸，run 停在 `running`，且 `run_pipeline` 抛出）。

- [ ] **Step 3a: flow_meta 防御式容错**

`flow_meta.py` 两个函数对非 dict 输入安全返回：

```python
def apply_input_mapping(meta: dict | None, upstream: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(meta, dict) or not isinstance(upstream, dict):
        return out
    for m in meta.get("inputMapping") or []:
        if not isinstance(m, dict):
            continue
        src, dst = m.get("from"), m.get("to")
        if src and dst and src in upstream:
            out[dst] = upstream[src]
    return out


def should_skip(meta: dict | None, ctx: dict[str, Any] | None) -> bool:
    if not isinstance(meta, dict):
        return False
    cond = meta.get("condition")
    if not isinstance(cond, dict) or not cond.get("field"):
        return False
    raw = None if not isinstance(ctx, dict) else ctx.get(cond["field"])
    actual = "" if raw is None else str(raw)
    expected = cond.get("value", "")
    op = cond.get("op") or "eq"
    if op == "neq":
        met = actual != expected
    elif op == "contains":
        met = str(expected) in actual
    else:
        met = actual == expected
    return not met
```

（同时修复 Phase 5 的 contains 数字误配：`str(expected) in actual`。）

- [ ] **Step 3b: run_pipeline 顶层兜底**

把 run_pipeline 的"节点循环 + 聚合 + 写回"整体包一层 try，未捕获异常一律把 run 落 `failed`：

```python
def run_pipeline(run_id: int, session_factory: SessionFactory) -> None:
    try:
        _run_pipeline_inner(run_id, session_factory)
    except Exception:
        logger.exception("pipeline run %s crashed at top level", run_id)
        db = session_factory()
        try:
            run = db.get(PipelineRun, run_id)
            if run is not None and run.status in ("pending", "running"):
                run.status = "failed"
                run.error_message = "执行器内部异常，运行已中止"
                run.completed_at = utcnow()
                db.commit()
        finally:
            db.close()
```

把原 `run_pipeline` 函数体改名为 `_run_pipeline_inner`（签名不变）。

- [ ] **Step 4: 跑测试确认通过 + 既有加固测试仍绿**

Run: `python -m pytest server/tests/test_pipeline_executor_hardening.py server/tests/test_pipeline_logic.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/pipelines/executor.py server/app/modules/pipelines/flow_meta.py server/tests/test_pipeline_executor_hardening.py
git commit -m "fix(pipelines): run_pipeline 顶层兜底 + flow_meta 容错，杜绝卡 running"
```

---

## Task 4: 前端轮询切换竞态（无测试运行器，typecheck + 手动验收）

**根因**：`PipelineEditor` 单例 `pollRef` + async tick，切换工作流后在途请求回填错状态 / 误清新 interval；且 `PipelinesWorkspace` 渲染无 `key`。

**Files:**
- Modify: `web/src/features/pipelines/PipelineEditor.tsx`（`onRun` 轮询 + 守卫）
- Modify: `web/src/features/pipelines/PipelinesWorkspace.tsx:63`（加 `key`）

- [ ] **Step 1: 给 PipelineEditor 强制按 pipeline 重挂载（消除跨切换状态残留）**

`PipelinesWorkspace.tsx` 第 63 行：

```tsx
// 旧
<PipelineEditor pipelineId={selectedId} onChanged={reload} />
// 新：key 让切换工作流时重挂载，runStatus/pollRef 随之重置
<PipelineEditor key={selectedId} pipelineId={selectedId} onChanged={reload} />
```

- [ ] **Step 2: onRun 轮询闭包捕获自身 interval，回填前守卫**

`PipelineEditor.tsx` 的 `onRun` 改为局部 `timer`，并校验仍是当前轮询：

```tsx
const onRun = async () => {
  try {
    const { run_id } = await startRun(pipelineId);
    setRunStatus("running");
    if (pollRef.current != null) { clearInterval(pollRef.current); pollRef.current = null; }
    let failures = 0;
    const timer = window.setInterval(async () => {
      // 只有当本 timer 仍是当前轮询时才处理（防切换/重跑后的脏写）
      if (pollRef.current !== timer) { clearInterval(timer); return; }
      try {
        const r = await getRun(run_id);
        if (pollRef.current !== timer) { clearInterval(timer); return; }
        failures = 0;
        setRunStatus(`${r.status}（文章 ${r.article_ids.length} 篇）`);
        if (["done", "failed", "partial_failed"].includes(r.status)) {
          clearInterval(timer);
          if (pollRef.current === timer) pollRef.current = null;
        }
      } catch {
        failures += 1;
        if (failures >= 5) {
          clearInterval(timer);
          if (pollRef.current === timer) { pollRef.current = null; setRunStatus("运行状态获取失败，请刷新"); }
        }
      }
    }, 1500);
    pollRef.current = timer;
  } catch (e) {
    toast(e instanceof Error ? e.message : "运行失败", "error");
  }
};
```

- [ ] **Step 3: 类型检查**

Run: `pnpm --filter @geo/web typecheck`
Expected: 无错误。

- [ ] **Step 4: 手动验收（记录到 PR 描述）**

1. 工作流 A 点"运行"，立即切到工作流 B → B 的状态行不得出现 A 的状态。
2. A 运行中切到 B 再点 B"运行" → B 状态行正常更新到终态，不卡 running。
3. 单工作流跑到 done/failed → 轮询停止（Network 面板不再轮询 `/runs/{id}`）。

- [ ] **Step 5: Commit**

```bash
git add web/src/features/pipelines/PipelineEditor.tsx web/src/features/pipelines/PipelinesWorkspace.tsx
git commit -m "fix(web): 修复工作流切换时轮询脏写/误清 interval 竞态（key + timer 守卫）"
```

---

## Task 5: 「编辑流程」跳转携带 pipeline id

**Files:**
- Modify: `web/src/App.tsx`（提升 selectedPipelineId + 透传）
- Modify: `web/src/features/pipelines/PipelinesWorkspace.tsx`（接收 `selectedId` prop）

- [ ] **Step 1: PipelinesWorkspace 接受外部初始选中**

```tsx
export function PipelinesWorkspace({ selectedId: externalId }: { selectedId?: number | null } = {}) {
  const [selectedId, setSelectedId] = useState<number | null>(externalId ?? null);
  useEffect(() => { if (externalId != null) setSelectedId(externalId); }, [externalId]);
  // ...其余不变
}
```

- [ ] **Step 2: App.tsx 提升状态并透传**

```tsx
const [editFlowId, setEditFlowId] = useState<number | null>(null);
// ...
<AgentManagementWorkspace onEditFlow={(id) => { setEditFlowId(id); handleNavClick("pipelines"); }} />
// ...pipelines tab 渲染处：
<PipelinesWorkspace selectedId={editFlowId} />
```

- [ ] **Step 3: 类型检查**

Run: `pnpm --filter @geo/web typecheck`
Expected: 无错误。

- [ ] **Step 4: 手动验收**：智能体列表对第 N 个点"编辑流程" → 编排页选中的是第 N 个（非默认第一个）。

- [ ] **Step 5: Commit**

```bash
git add web/src/App.tsx web/src/features/pipelines/PipelinesWorkspace.tsx
git commit -m "fix(web): 编辑流程跳转携带 pipeline id，避免编辑错智能体"
```

---

# Phase 1 — 调度可靠性

## Task 6: 调度改"最近到点 slot"，修 >60s 漏跑 / 漂移漏跑

**根因**：`current_slot` 要求 `now.minute == minute`，轮询点漂移或间隔>60s 时永久错过。改为返回 `<= now` 的最近 slot，由 `last_scheduled_run_at` claim 去重。

**Files:**
- Modify: `server/app/modules/pipelines/schedule_calc.py`（新增 `last_due_slot`）
- Modify: `server/app/modules/pipelines/scheduler.py`（改用 `last_due_slot`）
- Test: `server/tests/test_agent_management.py`（追加纯逻辑 + 调度 DB 测试）

- [ ] **Step 1: 写失败测试（纯逻辑）**

```python
# server/tests/test_agent_management.py 追加
import datetime as dt
from zoneinfo import ZoneInfo
from server.app.modules.pipelines.schedule_calc import last_due_slot

_TZ = ZoneInfo("Asia/Shanghai")
def _local(y, mo, d, h, mi): return dt.datetime(y, mo, d, h, mi, tzinfo=_TZ)

def test_last_due_slot_hourly_returns_recent_even_when_minute_passed():
    slot = last_due_slot("hourly", 30, None, None, _local(2026, 6, 5, 9, 47))
    assert (slot.hour, slot.minute) == (9, 30)

def test_last_due_slot_hourly_wraps_prev_hour():
    slot = last_due_slot("hourly", 30, None, None, _local(2026, 6, 5, 9, 10))
    assert (slot.hour, slot.minute) == (8, 30)

def test_last_due_slot_daily_before_time_wraps_prev_day():
    slot = last_due_slot("daily", 30, 9, None, _local(2026, 6, 5, 8, 0))
    assert (slot.day, slot.hour, slot.minute) == (4, 9, 30)

def test_last_due_slot_none_kind():
    assert last_due_slot("none", None, None, None, _local(2026, 6, 5, 9, 0)) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_agent_management.py -q -k last_due_slot`
Expected: FAIL（`last_due_slot` 未定义）。

- [ ] **Step 3: 实现 last_due_slot**

`schedule_calc.py` 追加（保留 `current_slot` 供旧测试，不删）：

```python
def last_due_slot(
    kind: str, minute: int | None, hour: int | None, weekday: int | None, now: dt.datetime
) -> dt.datetime | None:
    """返回 <= now 的最近一个计划 slot（截到分钟）；none/未配置返回 None。
    与 current_slot 不同：不要求 now 恰好落在计划分钟，从而轮询漂移 / 间隔>60s 也不漏跑，
    由调度器结合 last_scheduled_run_at claim 去重保证每个 slot 只触发一次。
    依赖 GEO_SCHEDULER_TZ 为无 DST 时区（如 Asia/Shanghai）。"""
    if kind == "hourly":
        if minute is None:
            return None
        slot = now.replace(minute=minute, second=0, microsecond=0)
        if slot > now:
            slot -= dt.timedelta(hours=1)
        return slot
    if kind == "daily":
        if minute is None or hour is None:
            return None
        slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if slot > now:
            slot -= dt.timedelta(days=1)
        return slot
    if kind == "weekly":
        if minute is None or hour is None or weekday is None:
            return None
        days_back = (now.weekday() - weekday) % 7
        slot = (now - dt.timedelta(days=days_back)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if slot > now:
            slot -= dt.timedelta(days=7)
        return slot
    return None
```

- [ ] **Step 4: scheduler 改用 last_due_slot**

`scheduler.py`：把 import 与调用从 `current_slot` 改为 `last_due_slot`：

```python
from server.app.modules.pipelines.schedule_calc import last_due_slot, in_window
# ...
slot_local = last_due_slot(kind, minute, hour, weekday, now)
if slot_local is None or not in_window(w_start, w_end, now):
    continue
```

claim 逻辑（`last_scheduled_run_at < slot_utc`）与 `_to_utc_naive` 不变。

- [ ] **Step 5: 写调度 DB 回归测试（轮询分钟不匹配也触发）**

参照既有 `test_run_due_triggers_once_and_claims`，但把 `now` 设在计划分钟**之后**几分钟：

```python
@pytest.mark.mysql
def test_run_due_triggers_even_when_poll_minute_mismatch(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.db.session import SessionLocal
        from server.app.modules.pipelines import service as svc
        from server.app.modules.pipelines.models import PipelineNode
        from server.app.modules.pipelines.scheduler import run_due_pipelines_once

        db = SessionLocal()
        try:
            p = svc.create_pipeline(db, user_id=app.admin_user_id, name="hourly",
                                    description=None, schedule_kind="hourly", schedule_minute=30)
            db.add(PipelineNode(pipeline_id=p.id, node_type="input", name="in",
                                node_index=0, config={"question_text": "x"}, flow_meta=None))
            db.commit()
        finally:
            db.close()
        monkeypatch.setattr(  # 不真跑后台线程
            "server.app.modules.pipelines.scheduler.run_pipeline", lambda *a, **k: None)
        now = _local(2026, 6, 5, 9, 47)  # 计划分钟=30，轮询落在 47 —— 旧实现会漏
        assert run_due_pipelines_once(SessionLocal, now=now) == 1
        assert run_due_pipelines_once(SessionLocal, now=now) == 0  # 同 slot 不重复
    finally:
        app.cleanup()
```

- [ ] **Step 6: 跑全部调度测试**

Run: `python -m pytest server/tests/test_agent_management.py -q`
Expected: PASS（含旧 `current_slot` 测试不受影响）。

- [ ] **Step 7: Commit**

```bash
git add server/app/modules/pipelines/schedule_calc.py server/app/modules/pipelines/scheduler.py server/tests/test_agent_management.py
git commit -m "fix(pipelines): 调度改 last_due_slot，修 >60s 间隔/漂移漏跑"
```

---

## Task 7: scheduler claim 与 create_run 同事务，失败回滚

**根因**：`scheduler.py` 先 `commit()` 推进 `last_scheduled_run_at` 再 `create_run`；create_run 失败则槽被吞、不补跑。

**Files:**
- Modify: `server/app/modules/pipelines/scheduler.py`（合并事务）
- Test: `server/tests/test_agent_management.py`（追加）

- [ ] **Step 1: 写失败测试（create_run 抛错时 slot 不应被推进）**

```python
@pytest.mark.mysql
def test_claim_rolled_back_when_create_run_fails(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from server.app.db.session import SessionLocal
        from server.app.modules.pipelines import service as svc
        from server.app.modules.pipelines.models import Pipeline, PipelineNode
        from server.app.modules.pipelines import scheduler as sched

        db = SessionLocal()
        try:
            p = svc.create_pipeline(db, user_id=app.admin_user_id, name="hourly",
                                    description=None, schedule_kind="hourly", schedule_minute=30)
            db.add(PipelineNode(pipeline_id=p.id, node_type="input", name="in",
                                node_index=0, config={}, flow_meta=None))
            db.commit(); pid = p.id
        finally:
            db.close()

        def _boom(*a, **k):
            raise RuntimeError("simulated create_run failure")
        monkeypatch.setattr(sched, "create_run", _boom)

        now = _local(2026, 6, 5, 9, 47)
        assert sched.run_due_pipelines_once(SessionLocal, now=now) == 0

        db = SessionLocal()
        try:
            assert db.get(Pipeline, pid).last_scheduled_run_at is None  # 槽未被吞
        finally:
            db.close()
    finally:
        app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_agent_management.py::test_claim_rolled_back_when_create_run_fails -q`
Expected: FAIL（`last_scheduled_run_at` 已被推进）。

- [ ] **Step 3: 合并 claim 与 create_run 到一次提交**

`run_due_pipelines_once` 内 claim 块改为：claim 后**不**立即 commit，紧接着 create_run，二者一起 commit；任一失败则 rollback：

```python
            db = session_factory()
            try:
                has_nodes = db.query(PipelineNode.id).filter(PipelineNode.pipeline_id == pid).first()
                if has_nodes is None:
                    continue
                running = (
                    db.query(PipelineRun.id)
                    .filter(PipelineRun.pipeline_id == pid,
                            PipelineRun.status.in_(("pending", "running")))
                    .first()
                )
                if running is not None:
                    continue
                res = db.execute(
                    update(Pipeline).where(
                        Pipeline.id == pid,
                        (Pipeline.last_scheduled_run_at.is_(None))
                        | (Pipeline.last_scheduled_run_at < slot_utc),
                    ).values(last_scheduled_run_at=slot_utc)
                )
                if res.rowcount != 1:
                    db.rollback()
                    continue
                p = db.get(Pipeline, pid)
                run = create_run(db, pipeline_id=pid, user_id=p.user_id)
                db.commit()  # claim + run 一起提交
                run_id = run.id
            except Exception:
                db.rollback()  # claim 回滚，slot 可被下轮重试
                raise
            finally:
                db.close()
```

- [ ] **Step 4: 跑测试确认通过 + 既有 claim 测试仍绿**

Run: `python -m pytest server/tests/test_agent_management.py -q -k "claim or run_due"`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/pipelines/scheduler.py server/tests/test_agent_management.py
git commit -m "fix(pipelines): scheduler claim 与 create_run 合并事务，失败回滚不吞调度"
```

---

# Phase 2 — 迁移 / 数据兼容

## Task 8: 0040 `tags` 回填，避免旧行读 500

**Files:**
- Modify: `server/alembic/versions/0040_agent_fields.py`（upgrade 末尾加回填）
- Modify: `server/app/modules/pipelines/router.py`（`_to_read` 兜底）
- Test: `server/tests/test_pipelines_api.py`（追加）

- [ ] **Step 1: 写失败测试（tags=NULL 的行读取不 500）**

```python
@pytest.mark.mysql
def test_read_pipeline_with_null_tags(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        from sqlalchemy import text
        from server.app.db.session import SessionLocal
        from server.app.modules.pipelines import service as svc
        from server.app.modules.pipelines.router import _to_read

        db = SessionLocal()
        try:
            p = svc.create_pipeline(db, user_id=app.admin_user_id, name="t", description=None)
            db.commit()
            db.execute(text("UPDATE pipelines SET tags=NULL WHERE id=:i"), {"i": p.id})
            db.commit()
            db.refresh(p)
            data = _to_read(db, p)  # 旧实现：model_validate 抛 → 500
            assert data["tags"] == []
        finally:
            db.close()
    finally:
        app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_pipelines_api.py::test_read_pipeline_with_null_tags -q`
Expected: FAIL（Pydantic 校验 `None` 不是 `list[str]`）。

- [ ] **Step 3a: `_to_read` 读出兜底**

`router.py` `_to_read`：

```python
def _to_read(db: Session, p) -> dict:
    nodes = svc.list_nodes(db, p.id)
    data = PipelineRead.model_validate(p, from_attributes=True).model_dump() if (p.tags is not None) \
        else {**PipelineRead.model_validate({**p.__dict__, "tags": []}).model_dump()}
    # 简化稳妥写法见下（推荐替换为）：
```

推荐改为先把对象的 tags 规整再校验：

```python
def _to_read(db: Session, p) -> dict:
    if p.tags is None:
        p.tags = []  # 读出兜底，避免历史 NULL 行 500（不 commit）
    nodes = svc.list_nodes(db, p.id)
    data = PipelineRead.model_validate(p).model_dump()
    data["nodes"] = [
        {"node_type": n.node_type, "name": n.name, "node_index": n.node_index,
         "config": n.config or {}, "flow_meta": n.flow_meta}
        for n in nodes
    ]
    return data
```

- [ ] **Step 3b: 迁移补回填**

`0040_agent_fields.py` `upgrade()` 末尾追加（在所有 add_column 之后）：

```python
    op.execute("UPDATE pipelines SET tags = JSON_ARRAY() WHERE tags IS NULL")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_pipelines_api.py::test_read_pipeline_with_null_tags -q`
Expected: PASS。

- [ ] **Step 5: 迁移可应用性自查**

Run: `python -m pytest server/tests/test_pipeline_migration.py -q`
Expected: PASS（确保 0040 仍可 upgrade/downgrade）。

- [ ] **Step 6: Commit**

```bash
git add server/alembic/versions/0040_agent_fields.py server/app/modules/pipelines/router.py server/tests/test_pipelines_api.py
git commit -m "fix(pipelines): 0040 回填 tags=[] + 读出兜底，避免旧行 500"
```

---

## Task 9: PATCH 可显式清空 nullable 字段（时间窗等）

**根因**：`patch_pipeline` 对所有字段用 `is not None` 过滤，显式 `null` 被吞 → 设过时间窗清不掉。

**Files:**
- Modify: `server/app/modules/pipelines/service.py`（`patch_pipeline`）
- Test: `server/tests/test_agent_management.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.mysql
def test_patch_can_clear_window(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        import datetime as dt
        from server.app.db.session import SessionLocal
        from server.app.modules.pipelines import service as svc

        db = SessionLocal()
        try:
            p = svc.create_pipeline(db, user_id=app.admin_user_id, name="t", description=None,
                                    window_start=dt.time(9, 0), window_end=dt.time(18, 0))
            db.commit()
            svc.patch_pipeline(db, p, fields={"window_start": None, "window_end": None})
            db.commit()
            assert p.window_start is None and p.window_end is None
        finally:
            db.close()
    finally:
        app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_agent_management.py::test_patch_can_clear_window -q`
Expected: FAIL（None 被过滤，窗未清）。

- [ ] **Step 3: patch_pipeline 区分"未传"与"显式 null"**

定义可清空字段白名单，对其用"key 在 fields 即应用（含 None）"；其余保持 `is not None`：

```python
_NULLABLE_CLEARABLE = {"description", "window_start", "window_end",
                       "schedule_minute", "schedule_hour", "schedule_weekday"}

def patch_pipeline(db: Session, p: Pipeline, *, fields: dict) -> Pipeline:
    merged = {
        "name": p.name, "type": p.type, "tags": list(p.tags or []),
        "schedule_kind": p.schedule_kind, "schedule_minute": p.schedule_minute,
        "schedule_hour": p.schedule_hour, "schedule_weekday": p.schedule_weekday,
        "window_start": p.window_start, "window_end": p.window_end,
    }
    for k in merged:
        if k in fields and (fields[k] is not None or k in _NULLABLE_CLEARABLE):
            merged[k] = fields[k]
    validate_agent_fields(**merged)
    settable = ["name", "description", "type", "tags", "ignore_exception", "is_enabled",
                "schedule_kind", "schedule_minute", "schedule_hour", "schedule_weekday",
                "window_start", "window_end"]
    for k in settable:
        if k not in fields:
            continue
        if fields[k] is None and k not in _NULLABLE_CLEARABLE:
            continue
        if k == "name":
            setattr(p, k, fields[k].strip())
        elif k == "tags":
            setattr(p, k, _dedup_tags(fields[k]))
        else:
            setattr(p, k, fields[k])
    db.flush()
    return p
```

> 注意：`validate_agent_fields` 的窗校验 `(window_start is None) != (window_end is None)` 要求成对清空——本测试同时清两者，合法。

- [ ] **Step 4: 跑测试确认通过 + 既有 patch/tags 测试仍绿**

Run: `python -m pytest server/tests/test_agent_management.py -q -k "patch or tags"`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/pipelines/service.py server/tests/test_agent_management.py
git commit -m "fix(pipelines): PATCH 支持显式 null 清空时间窗/调度分钟等字段"
```

---

# Phase 3 — 韧性 / 并发

## Task 10: 启动恢复改为单 leader 持有（防多进程误杀）

**根因**：`recover_stuck_pipeline_runs`/`recover_stuck_scheme_runs` 无租约全量置 failed；多 web 进程下会打死别进程在跑的 run。当前 Dockerfile 单进程未触发，但加 worker 前必修。最小修法：用一个进程级开关 `GEO_RUN_STARTUP_RECOVERY`（默认 true）控制是否执行恢复，文档要求多实例时只在一个实例开启。

**Files:**
- Modify: `server/app/core/config.py`（加 `run_startup_recovery: bool = True`）
- Modify: `server/app/main.py`（恢复段用开关包裹）
- Modify: `DEPLOYMENT.md`（多实例须知）
- Test: `server/tests/test_pipeline_recovery.py`（追加：开关关闭时不复位）

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.mysql
def test_recovery_skipped_when_flag_off(monkeypatch):
    monkeypatch.setenv("GEO_RUN_STARTUP_RECOVERY", "false")
    from server.app.core.config import get_settings
    get_settings.cache_clear()
    assert get_settings().run_startup_recovery is False
    get_settings.cache_clear()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_pipeline_recovery.py::test_recovery_skipped_when_flag_off -q`
Expected: FAIL（`run_startup_recovery` 不存在）。

- [ ] **Step 3: 加配置 + main.py 包裹**

`config.py`：

```python
    run_startup_recovery: bool = True  # GEO_RUN_STARTUP_RECOVERY；多实例只在单一实例开启
```

`main.py` 恢复段（约 110-123）外层加：

```python
    if get_settings().run_startup_recovery:
        try:
            recover_db = SessionLocal()
            try:
                recover_stuck_records(recover_db)
                from server.app.modules.pipelines.recovery import recover_stuck_pipeline_runs
                recover_stuck_pipeline_runs(recover_db)
                from server.app.modules.ai_generation.scheme_executor import recover_stuck_scheme_runs
                recover_stuck_scheme_runs(recover_db)
            finally:
                recover_db.close()
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).exception("Startup recovery failed")
```

- [ ] **Step 4: DEPLOYMENT.md 加须知**

在 web 启动小节追加：

```markdown
> 多实例部署（gunicorn -w N / 多容器）必须只在**一个** web 实例设 `GEO_RUN_STARTUP_RECOVERY=true`，
> 其余设 `false`；同理 `GEO_PIPELINE_SCHEDULER_ENABLED` 只在单一实例开启。否则启动恢复会把
> 其它实例正在执行的 run 误判为僵死并置 failed。
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest server/tests/test_pipeline_recovery.py -q`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add server/app/core/config.py server/app/main.py DEPLOYMENT.md server/tests/test_pipeline_recovery.py
git commit -m "fix(pipelines): 启动恢复加 GEO_RUN_STARTUP_RECOVERY 开关 + 多实例须知，防误杀"
```

---

## Task 11: ai_generate.count 上限 + 全局 pipeline 并发闸

**Files:**
- Modify: `server/app/core/config.py`（加 `ai_generate_max_count`、`pipeline_max_concurrent_runs`）
- Modify: `server/app/modules/pipelines/nodes/ai_generate_node.py`（校验 count 上限）
- Modify: `server/app/modules/pipelines/executor.py`（全局信号量包裹 `_run_pipeline_inner`）
- Test: `server/tests/test_pipeline_logic.py`（count 上限）

- [ ] **Step 1: 写失败测试（count 超限抛 ValidationError）**

```python
def test_ai_generate_rejects_excessive_count():
    from server.app.modules.pipelines.nodes.ai_generate_node import run_ai_generate
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.shared.errors import ValidationError
    import pytest
    ctx = NodeRunContext(session_factory=lambda: None, user_id=1,
                         config={"prompt_template_id": 1, "count": 9999, "question_text": "x"},
                         inputs={}, upstream={})
    with pytest.raises(ValidationError):
        run_ai_generate(ctx)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_pipeline_logic.py::test_ai_generate_rejects_excessive_count -q`
Expected: FAIL（无上限校验，会越过进 DB）。

- [ ] **Step 3a: 配置项**

`config.py`：

```python
    ai_generate_max_count: int = 20      # GEO_AI_GENERATE_MAX_COUNT
    pipeline_max_concurrent_runs: int = 3  # GEO_PIPELINE_MAX_CONCURRENT_RUNS
```

- [ ] **Step 3b: ai_generate_node 校验上限（在取 count 之后）**

```python
    count = int(cfg.get("count") or 0)
    from server.app.core.config import get_settings
    max_count = get_settings().ai_generate_max_count
    if not template_id or count <= 0:
        raise ValidationError("ai_generate 节点需配置 prompt_template_id 与 count>0")
    if count > max_count:
        raise ValidationError(f"生成数量超过上限 {max_count}")
```

- [ ] **Step 3c: executor 全局信号量**

`executor.py` 顶部加模块级信号量，`run_pipeline` 内对 `_run_pipeline_inner` 包裹：

```python
import threading as _threading
from server.app.core.config import get_settings as _get_settings
_RUN_SEMAPHORE = _threading.Semaphore(max(1, _get_settings().pipeline_max_concurrent_runs))

# run_pipeline 内：
    with _RUN_SEMAPHORE:
        _run_pipeline_inner(run_id, session_factory)
```

（信号量按进程；与 Task 10 的单 leader 约束配合即为全局上限。）

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_pipeline_logic.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add server/app/core/config.py server/app/modules/pipelines/nodes/ai_generate_node.py server/app/modules/pipelines/executor.py server/tests/test_pipeline_logic.py
git commit -m "fix(pipelines): ai_generate.count 上限 + 全局 run 并发信号量，防打满 DB/LLM"
```

---

## Task 12: PipelineRun 冻结快照（执行版本可追溯）

**根因**：`run_pipeline` 线程启动时才读 live nodes；创建到执行之间若有人 publish，跑的非点击版本。修法：`create_run` 时把当前 live 节点序列化进 `PipelineRun.snapshot`，executor 优先读 snapshot。

**Files:**
- Modify: `server/app/modules/pipelines/models.py`（`PipelineRun` 加 `snapshot` JSON 列）
- Create: `server/alembic/versions/0041_pipeline_run_snapshot.py`
- Modify: `server/app/modules/pipelines/executor.py`（`create_run` 写 snapshot；`_run_pipeline_inner` 优先读 snapshot）
- Test: `server/tests/test_pipeline_executor_hardening.py`（追加）

- [ ] **Step 1: 加列 + 迁移**

`models.py` `PipelineRun` 追加：

```python
    snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
```

新建 `0041_pipeline_run_snapshot.py`（`revision="0041"`, `down_revision="0040"`）：

```python
def upgrade() -> None:
    op.add_column("pipeline_runs", sa.Column("snapshot", sa.JSON(), nullable=True))
def downgrade() -> None:
    op.drop_column("pipeline_runs", "snapshot")
```

- [ ] **Step 2: create_run 写 snapshot + executor 读 snapshot**

`executor.py` `create_run` 在 `db.add(run)` 前，用 `nodes_to_snapshot(list_nodes(...))` 填充 `snapshot`；`_run_pipeline_inner` 构造 `node_specs` 时优先用 `run.snapshot`，无则回退当前 live 节点（兼容旧 run）。

```python
from server.app.modules.pipelines.snapshot import nodes_to_snapshot, snapshot_to_node_dicts
# create_run:
    from server.app.modules.pipelines.service import list_nodes
    snap = nodes_to_snapshot(list_nodes(db, pipeline_id))
    run = PipelineRun(pipeline_id=pipeline_id, user_id=user_id, status="pending",
                      node_results={}, article_ids=[], snapshot=snap)
# _run_pipeline_inner，读节点处：
    if run.snapshot:
        node_specs = [
            {"node_type": d["node_type"], "node_index": d["node_index"],
             "config": d.get("config") or {}, "flow_meta": d.get("flow_meta")}
            for d in snapshot_to_node_dicts(run.snapshot)
        ]
    else:
        # 旧 run 回退：读 live 节点（原逻辑）
        ...
```

- [ ] **Step 3: 写测试（publish 改节点后，已建 run 仍跑旧 snapshot）**

```python
@pytest.mark.mysql
def test_run_uses_frozen_snapshot(monkeypatch):
    # 建 pipeline 发布 v1（input 节点）→ create_run → publish v2（改 config）
    # → 断言 run.snapshot 仍为 v1 的节点配置
    ...
```

（按 `test_pipeline_executor_hardening.py` 既有 fixture 填充具体断言。）

- [ ] **Step 4: 跑测试 + 迁移测试**

Run: `python -m pytest server/tests/test_pipeline_executor_hardening.py server/tests/test_pipeline_migration.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/pipelines/models.py server/alembic/versions/0041_pipeline_run_snapshot.py server/app/modules/pipelines/executor.py server/tests/test_pipeline_executor_hardening.py
git commit -m "feat(pipelines): PipelineRun 冻结执行快照，运行版本可追溯"
```

---

## Task 13: scheduler 查询补索引

**Files:**
- Create: `server/alembic/versions/0042_pipeline_schedule_index.py`
- Test: `server/tests/test_pipeline_migration.py`（迁移可应用）

- [ ] **Step 1: 迁移加复合索引**

`0042`（`down_revision="0041"`）：

```python
def upgrade() -> None:
    op.create_index("ix_pipelines_enabled_kind", "pipelines", ["is_enabled", "schedule_kind"])
def downgrade() -> None:
    op.drop_index("ix_pipelines_enabled_kind", table_name="pipelines")
```

- [ ] **Step 2: 跑迁移测试**

Run: `python -m pytest server/tests/test_pipeline_migration.py -q`
Expected: PASS。

- [ ] **Step 3: Commit**

```bash
git add server/alembic/versions/0042_pipeline_schedule_index.py
git commit -m "perf(pipelines): scheduler 扫描列 (is_enabled, schedule_kind) 加索引"
```

---

# Phase 4 — 治理 / 边界

## Task 14: 跨午夜时间窗支持（或明示拒绝）

**Files:**
- Modify: `server/app/modules/pipelines/service.py`（`validate_agent_fields` 放开 start<end 限制）
- Modify: `server/app/modules/pipelines/schedule_calc.py`（`in_window` 支持跨夜）
- Test: `server/tests/test_agent_management.py`

- [ ] **Step 1: 写失败测试**

```python
def test_in_window_overnight():
    from server.app.modules.pipelines.schedule_calc import in_window
    import datetime as dt
    ws, we = dt.time(22, 0), dt.time(6, 0)
    assert in_window(ws, we, _local(2026, 6, 5, 23, 0)) is True
    assert in_window(ws, we, _local(2026, 6, 5, 3, 0)) is True
    assert in_window(ws, we, _local(2026, 6, 5, 12, 0)) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_agent_management.py::test_in_window_overnight -q`
Expected: FAIL。

- [ ] **Step 3: in_window 支持跨夜 + 放开校验**

`schedule_calc.py`：

```python
def in_window(window_start, window_end, now) -> bool:
    if window_start is None or window_end is None:
        return True
    t = now.timetz().replace(tzinfo=None)
    if window_start <= window_end:
        return window_start <= t <= window_end
    return t >= window_start or t <= window_end  # 跨午夜
```

`service.py` `validate_agent_fields` 删除 `window_start < window_end` 强制（保留成对设置校验），允许跨夜窗。

- [ ] **Step 4: 跑测试 + 既有窗校验测试调整**

Run: `python -m pytest server/tests/test_agent_management.py -q -k window`
Expected: PASS（同步更新 `test_validate_window_order`，不再要求拒绝跨夜）。

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/pipelines/service.py server/app/modules/pipelines/schedule_calc.py server/tests/test_agent_management.py
git commit -m "feat(pipelines): 时间窗支持跨午夜（22:00-06:00）"
```

---

## Task 15: 定时管道跨租户分发——显式决策（文档 + 校验）

**决策点（需团队确认，二选一）**：
- (A) 定时触发的 distribute 仅允许使用**属主自己**的账号/分组，即使属主是 admin（调度路径强制 `user_id_filter=p.user_id`）。
- (B) 维持 admin 全局权限，但在 `DEPLOYMENT.md`/产品文档明确"admin 拥有的定时管道会跨租户分发"。

**Files（若选 A）:**
- Modify: `server/app/modules/pipelines/nodes/distribute_node.py`（调度上下文传 `enforce_owner=True`，create_task 用属主 id 过滤）
- Test: `server/tests/test_pipeline_review_distribute.py`

- [ ] **Step 1: 与团队确认 A/B（在 PR 描述记录决议）**
- [ ] **Step 2（若 A）：distribute 节点在 NodeRunContext 增 `is_scheduled` 标志，调度路径置 True，分发时强制按属主过滤**
- [ ] **Step 3: 对应测试 + commit**

```bash
git commit -m "fix(pipelines): 定时管道分发按属主限定（或文档化 admin 跨租户语义）"
```

---

# Phase 5 — 技术债 / 打磨

> 以下为低风险局部修复，可合并为少数 commit。每条均"改 + 跑相关测试 + 提交"。

## Task 16: done→partial_failed 状态闪烁——成组并入终态前

**Files:** `server/app/modules/pipelines/executor.py`
- [ ] 把"产出文章 → 成组/送审"的结果在**第一次写终态之前**纳入：先做成组，拿到 gid，再一次性写 `status`（done/partial_failed）+ `error_message` + `completed_at`，删除事后二次降级的 commit。
- [ ] Run: `python -m pytest server/tests/test_pipeline_grouping.py -q` → PASS
- [ ] Commit: `fix(pipelines): run 终态一次写定，消除 done→partial_failed 闪烁`

## Task 17: 统一 mark_pending_and_group 失败语义

**Files:** `server/app/modules/ai_generation/scheme_executor.py`
- [ ] `_group_run_articles` 检查返回值：None 时把 scheme run 由 `done` 降级 `partial_failed` 并写 `error_message`（与 pipeline 对齐）。
- [ ] Run: `python -m pytest server/tests/test_scheme_runs.py -q` → PASS
- [ ] Commit: `fix(ai-gen): scheme 成组失败时与 pipeline 一致降级 run 状态`

## Task 18: 抽公共 recovery + scheduler 脚手架（去重）

**Files:** 新建 `server/app/shared/background.py`
- [ ] 实现 `recover_stuck_runs(db, model, *, statuses=("running","pending"), terminal="failed", message=...)` 与 `IntervalDaemon`（封装 `_stop`/`_thread`/`start`/`stop`/`_loop`）。
- [ ] `pipelines/recovery.py`、`scheme_executor.recover_stuck_scheme_runs`、`pipelines/scheduler.py`、`sync_scheduler.py` 改为委托公共实现（行为不变）。
- [ ] Run: `python -m pytest server/tests/test_pipeline_recovery.py server/tests/test_scheme_recovery.py server/tests/test_agent_management.py -q` → PASS
- [ ] Commit: `refactor(shared): 抽 recover_stuck_runs + IntervalDaemon，消除三处 recovery/两处 scheduler 重复`

## Task 19: _next_version_no 改 DB 聚合

**Files:** `server/app/modules/pipelines/service.py:245-253`
- [ ] 改 `db.query(func.max(PipelineVersion.version_no)).filter(...).scalar()`，`return (max_no or 0) + 1`。
- [ ] Run: `python -m pytest server/tests/test_pipeline_service.py -q` → PASS
- [ ] Commit: `perf(pipelines): _next_version_no 改 SELECT MAX`

## Task 20: 缩小 publish_draft 行锁范围

**Files:** `server/app/modules/pipelines/service.py`（`publish_draft`）
- [ ] 把 `with_for_update` 仅保护"读最大版本号 + 插入 PipelineVersion + 切 has_draft"；节点重建（delete/insert）移到锁外（节点属同一 pipeline 草稿，并发发布已被版本号唯一约束兜底）。
- [ ] Run: `python -m pytest server/tests/test_pipeline_service.py server/tests/test_pipeline_template.py -q` → PASS
- [ ] Commit: `perf(pipelines): publish_draft 行锁仅覆盖版本号分配`

## Task 21: scheme_router bg=None 与 pipelines 对齐

**Files:** `server/app/modules/ai_generation/scheme_router.py:261-263`
- [ ] `bg_session_factory is None` 时：把 run 置 `failed` + `error_message` 并返回 503（对齐 `pipelines/router.py`）。
- [ ] Run: `python -m pytest server/tests/test_scheme_runs.py -q` → PASS
- [ ] Commit: `fix(ai-gen): scheme run bg 未注入时标 failed+503，与 pipelines 一致`

## Task 22: article_group_source 读取上游 inputs

**Files:** `server/app/modules/pipelines/nodes/article_group_source.py`
- [ ] `group_id = ctx.inputs.get("group_id") or (ctx.config or {}).get("group_id")`（与其它节点一致）。
- [ ] Run: `python -m pytest server/tests/test_pipeline_review_distribute.py -q` → PASS
- [ ] Commit: `fix(pipelines): article_group_source 支持上游 inputMapping 注入 group_id`

## Task 23: 模型/迁移 JSON nullable 对齐

**Files:** `server/app/modules/pipelines/models.py`
- [ ] 把 `pipeline_nodes.config`、`pipeline_runs.node_results/article_ids`、`pipelines.tags` 的模型声明改 `nullable=True`（与迁移一致；MySQL JSON 无 server_default）。
- [ ] Run: `python -m pytest server/tests/test_pipeline_migration.py -q` → PASS
- [ ] Commit: `chore(pipelines): 模型 JSON 字段 nullable 与迁移对齐，止住 autogenerate 抖动`

## Task 24: 临时封面兜底配置化（技术债）

**Files:** `server/app/modules/ai_generation/scheme_executor.py:347-415`、`config.py`
- [ ] `_TEMP_COVER_BUCKET` 改为读 `GEO_TEMP_COVER_BUCKET`（默认空=禁用）；`order_by(func.rand())` 改预采样 id（先取 count 再随机 offset，或取 id 列表随机选）。空配置时整段跳过。
- [ ] Run: `python -m pytest server/tests/test_scheme_runs.py -q` → PASS
- [ ] Commit: `refactor(ai-gen): 临时封面 bucket 配置化 + 去 ORDER BY RAND()`

## Task 25: 补测试盲区 + 去测试 helper 重复

**Files:** 新建 `server/tests/pipeline_helpers.py`；相关测试文件改 import
- [ ] 把 `test_pipeline_review_distribute.py` / `test_pipeline_executor_hardening.py` 重复的 `_write_storage_state`/`_create_account`/`_set_review_status`/`_make_article` 提取到 `pipeline_helpers.py`。
- [ ] 补：并发 10× `POST /{id}/runs` → 1×202 + 9×409；publish 与 run 并发无死锁；运行中 overlap 闸跳过。
- [ ] Run: `python -m pytest server/tests/test_pipeline_*.py server/tests/test_agent_management.py -q` → PASS
- [ ] Commit: `test(pipelines): 去 helper 重复 + 补并发/overlap/漂移盲区`

---

## 已剔除的假阳性（**不在本计划内，勿做**）

- DeepSeek「onRun 失败卡 running」：误读求值顺序，`setRunStatus("running")` 在 `await startRun()` 之后，失败时不执行。
- DeepSeek「distribute 空列表 falsy 陷阱」：当前逻辑正确，属假想重构风险。
- DeepSeek「`type` 参数遮蔽内建 = P0」：ruff 配置(E/F/I/B/UP)不含 A002，至多风格项（如需可并入某次 commit 改名 `agent_type`，不单列任务）。
- DeepSeek「IntegrityError rollback 后复用 session = P0」：SQLAlchemy rollback 即 expire，`db.get` 重查，非拿旧缓存。
- DeepSeek「第三处 ThreadPoolExecutor(`pipeline.py:151`)」：属已 410 下线休眠代码，勿当活跃重复处理。
- 预留 endpoint `get_version`/`list_runs`：有"勿删"注释，Task 25 补测试覆盖即可，不删。

---

## Self-Review（计划自检）

- **Spec 覆盖**：T1（前端竞态/编辑跳转/审核绕过/节点崩溃）→ Task 1-5；T2（调度漂移/claim 事务）→ Task 6-7；迁移/PATCH → Task 8-9；T3（恢复/并发/快照/索引）→ Task 10-13；T4 治理/边界 → Task 14-15；技术债 → Task 16-25。整改清单 25 条全部有对应任务。
- **类型/命名一致**：`_run_pipeline_inner`（Task 3 引入）在 Task 11/12 复用同名；`last_due_slot`（Task 6）在 Task 7 测试沿用；`run_startup_recovery`/`pipeline_max_concurrent_runs`/`ai_generate_max_count`/`GEO_TEMP_COVER_BUCKET` 配置命名前后一致。
- **占位符扫描**：Task 12/15 的部分断言标注"按既有 fixture 填充"，因依赖具体测试夹具结构，执行时以 `server/tests/utils.py` 与既有同类测试为准——非逻辑占位，而是夹具适配说明。
- **迁移链**：0040 →（Task 12）0041 →（Task 13）0042，线性单头；执行 Task 12/13 时确认 `down_revision` 串接当时最新 head。
