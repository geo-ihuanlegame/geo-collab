# 智能体日志 分页+滚动+日期筛选 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给已上线的「智能体 · 日志」页加 服务端按日志行分页（20/30 每页）、起止日期筛选（北京日历日）、页内固定高度滚动（表头吸顶）。

**Architecture:** 后端 `GET /api/pipelines/{id}/logs` 增 `page`/`page_size`/`start_date`/`end_date`，响应改 `{items,total,page,page_size}` envelope；分页用 `JSON_LENGTH` 累加游走（A 方案），只加载落在当前页窗口内那几条 run 的 `node_results`。前端 `AgentLogsView` 加筛选栏 + 滚动容器 + 分页器。仍从 `PipelineRun` 派生，不加表、不改执行引擎、不写迁移。

**Tech Stack:** FastAPI + SQLAlchemy（MySQL only，用 `func.json_length`）、pytest（`@pytest.mark.mysql` 真库）、React 19 + TypeScript + Vite。

**约定：**
- 所有 commit 走仓库习惯（`feat(pipelines):` / `test(pipelines):` 等），并由执行者按仓库规范在消息末尾追加 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。
- 后端测试需要 `GEO_TEST_DATABASE_URL`（库名含 `test`）。Windows PowerShell：`$env:GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test"; pytest ...`。`@pytest.mark.mysql` 用例未设此变量会自动跳过。
- 参考既有范式：`web/src/features/system/AuditLogsWorkspace.tsx`（筛选/分页/滚动）、`server/app/modules/audit/service.py`（日期/分页）。

---

## File Structure

- **Modify** `server/app/modules/pipelines/run_logs.py` — 新增纯函数 `beijing_day_to_utc_range` + DB 函数 `list_run_log_page`；`build_run_log_rows` 不动。
- **Modify** `server/app/modules/pipelines/schemas.py` — 新增 `RunLogPage`。
- **Modify** `server/app/modules/pipelines/router.py` — 重写 `list_run_logs` 端点（参数 + envelope）。
- **Modify** `server/tests/test_pipeline_logs.py` — 改 3 个现有端点测试读 `["items"]`；扩展 `_add_run` 支持 `completed_at`；加 helper 单测 + 5 个新端点测试。
- **Modify** `web/src/types.ts` — 新增 `RunLogPage` 类型。
- **Modify** `web/src/api/pipelines.ts` — `listPipelineLogs` 改签名（opts 对象 + envelope）。
- **Modify** `web/src/features/pipelines/AgentLogsView.tsx` — 加筛选栏 / 滚动容器 / 分页器。
- **Modify** `web/src/styles.css` — 新增 `.agentLogsFilter` / `.agentLogsScroll`（吸顶表头）/ `.agentLogsPager`。

---

## Task 1: `beijing_day_to_utc_range` 纯函数（北京日 → 朴素 UTC 区间）

**Files:**
- Modify: `server/app/modules/pipelines/run_logs.py`
- Test: `server/tests/test_pipeline_logs.py`

- [ ] **Step 1: Write the failing test**

在 `server/tests/test_pipeline_logs.py` 顶部（紧跟现有 import 之后、`test_build_run_log_rows_levels_and_order` 之前）加：

```python
def test_beijing_day_to_utc_range():
    from server.app.modules.pipelines.run_logs import beijing_day_to_utc_range

    # 北京 2026-06-05 这一天 → UTC [2026-06-04 16:00, 2026-06-05 16:00)
    start_dt, end_dt = beijing_day_to_utc_range("2026-06-05", "2026-06-05")
    assert start_dt == datetime(2026, 6, 4, 16, 0, 0)
    assert end_dt == datetime(2026, 6, 5, 16, 0, 0)

    # 仅 start / 仅 end / 都空
    s_only, e_none = beijing_day_to_utc_range("2026-06-05", None)
    assert s_only == datetime(2026, 6, 4, 16, 0, 0) and e_none is None
    n_start, e_only = beijing_day_to_utc_range(None, "2026-06-05")
    assert n_start is None and e_only == datetime(2026, 6, 5, 16, 0, 0)
    assert beijing_day_to_utc_range(None, None) == (None, None)


def test_beijing_day_to_utc_range_bad_format():
    import pytest as _pytest

    from server.app.modules.pipelines.run_logs import beijing_day_to_utc_range

    with _pytest.raises(ValueError):
        beijing_day_to_utc_range("2026-13-99", None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_pipeline_logs.py::test_beijing_day_to_utc_range -q`
Expected: FAIL — `ImportError: cannot import name 'beijing_day_to_utc_range'`.

- [ ] **Step 3: Write minimal implementation**

在 `server/app/modules/pipelines/run_logs.py` 改文件头并加函数。新的文件头（替换现有 1-3 行）：

```python
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func

from server.app.modules.pipelines.schemas import RunLogRow

_BEIJING_OFFSET = timedelta(hours=8)
```

在 `build_run_log_rows` 下方新增：

```python
def beijing_day_to_utc_range(
    start_date: str | None, end_date: str | None
) -> tuple[datetime | None, datetime | None]:
    """把北京日历日 YYYY-MM-DD 起止转成朴素 UTC 的半开区间 [start, end)。

    end_date 取「次日北京零点」作为开区间上界。解析失败抛 ValueError（调用方转 400）。
    产品 China-only，固定 +08:00（与前端 fmtTime 的 Asia/Shanghai 一致）。
    """
    start_dt = None
    end_dt = None
    if start_date:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d") - _BEIJING_OFFSET
    if end_date:
        end_dt = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)) - _BEIJING_OFFSET
    return start_dt, end_dt
```

> 注意 `func` 此步未用到，但下一任务的 `list_run_log_page` 要用；先 import 会触发 ruff `F401`，所以**本步先不要加 `from sqlalchemy import func`**——把它留到 Task 2 Step 3 一起加。本步文件头只加 `from datetime import datetime, timedelta` 与 `_BEIJING_OFFSET`。

修正：本步文件头实际写成（不含 `func`）：

```python
from __future__ import annotations

from datetime import datetime, timedelta

from server.app.modules.pipelines.schemas import RunLogRow

_BEIJING_OFFSET = timedelta(hours=8)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest server/tests/test_pipeline_logs.py::test_beijing_day_to_utc_range server/tests/test_pipeline_logs.py::test_beijing_day_to_utc_range_bad_format -q`
Expected: PASS（2 passed）。

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/pipelines/run_logs.py server/tests/test_pipeline_logs.py
git commit -m "feat(pipelines): beijing_day_to_utc_range 北京日历日→UTC 区间 纯函数"
```

---

## Task 2: 端点分页 + 日期筛选（`list_run_log_page` + `RunLogPage` + 重写端点）

**Files:**
- Modify: `server/app/modules/pipelines/schemas.py`（加 `RunLogPage`）
- Modify: `server/app/modules/pipelines/run_logs.py`（加 `list_run_log_page` + `func` import）
- Modify: `server/app/modules/pipelines/router.py`（重写 `list_run_logs`）
- Test: `server/tests/test_pipeline_logs.py`

- [ ] **Step 1: Write/界定 failing tests**

(a) 把 `_add_run` 扩展为支持 `completed_at`（替换现有 `_add_run` 整个函数）：

```python
def _add_run(app, pid, node_results, status="partial_failed", completed_at=None):
    from server.app.modules.pipelines.models import PipelineRun
    from server.app.modules.system.models import User

    with app.session_factory() as db:
        uid = db.query(User).first().id
        run = PipelineRun(
            pipeline_id=pid,
            user_id=uid,
            status=status,
            node_results=node_results,
            article_ids=[],
            completed_at=completed_at,
        )
        db.add(run)
        db.commit()
        return run.id
```

(b) 改 3 个现有端点测试读 envelope。

`test_logs_endpoint_flattens_run` 中 `rows = client.get(...).json()` 改为：

```python
        body = client.get(f"/api/pipelines/{pid}/logs").json()
        rows = body["items"]
        assert body["total"] == 3
```

（其余对 `rows[...]` 的断言不变。）

`test_logs_newest_batch_first` 中 `rows = client.get(...).json()` 改为：

```python
        rows = client.get(f"/api/pipelines/{pid}/logs").json()["items"]
```

`test_logs_empty_and_not_found` 改为：

```python
@pytest.mark.mysql
def test_logs_empty_and_not_found(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pid = client.post("/api/pipelines", json={"name": "空日志", "type": "general"}).json()["id"]
        body = client.get(f"/api/pipelines/{pid}/logs").json()
        assert body["items"] == [] and body["total"] == 0  # 无 run
        assert client.get("/api/pipelines/999999/logs").status_code == 404  # _owned 守卫
    finally:
        app.cleanup()
```

(c) 在文件末尾追加 5 个新端点测试：

```python
@pytest.mark.mysql
def test_logs_paginate_by_row_across_batches(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pid = _publish_three_node_pipeline(client)
        # 8 批 × 每批 3 行 = 24 行，id 递增 → 最新（高 id）在前
        ids = [
            _add_run(app, pid, {"0": {"ok": 1}, "1": {"ok": 1}, "2": {"ok": 1}}, status="done")
            for _ in range(8)
        ]
        p1 = client.get(f"/api/pipelines/{pid}/logs?page=1&page_size=20").json()
        p2 = client.get(f"/api/pipelines/{pid}/logs?page=2&page_size=20").json()
        assert p1["total"] == 24 and p2["total"] == 24
        assert len(p1["items"]) == 20 and len(p2["items"]) == 4
        combined = p1["items"] + p2["items"]
        keys = [(r["batch"], r["step"]) for r in combined]
        assert len(keys) == 24 and len(set(keys)) == 24  # 不重不漏
        # 第 7 新批次 = ids[1]，其 step1 被切到第 1 页末、step2 在第 2 页首（边界连续）
        assert p1["items"][-1]["batch"] == ids[1] and p1["items"][-1]["step"] == 1
        assert p2["items"][0]["batch"] == ids[1] and p2["items"][0]["step"] == 2
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_logs_date_range_beijing_boundary(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pid = _publish_three_node_pipeline(client)
        # A：北京 2026-06-04 23:30 = UTC 06-04 15:30；B：北京 2026-06-05 00:30 = UTC 06-04 16:30
        a = _add_run(app, pid, {"0": {"ok": 1}}, status="done",
                     completed_at=datetime(2026, 6, 4, 15, 30))
        b = _add_run(app, pid, {"0": {"ok": 1}}, status="done",
                     completed_at=datetime(2026, 6, 4, 16, 30))
        d5 = client.get(f"/api/pipelines/{pid}/logs?start_date=2026-06-05&end_date=2026-06-05").json()
        assert [r["batch"] for r in d5["items"]] == [b] and d5["total"] == 1
        d4 = client.get(f"/api/pipelines/{pid}/logs?start_date=2026-06-04&end_date=2026-06-04").json()
        assert [r["batch"] for r in d4["items"]] == [a] and d4["total"] == 1
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_logs_page_size_normalized(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pid = _publish_three_node_pipeline(client)
        for _ in range(8):
            _add_run(app, pid, {"0": {"ok": 1}, "1": {"ok": 1}, "2": {"ok": 1}}, status="done")
        # page_size=7 非法 → 归 30
        r = client.get(f"/api/pipelines/{pid}/logs?page_size=7").json()
        assert r["page_size"] == 30 and len(r["items"]) == 24
        r20 = client.get(f"/api/pipelines/{pid}/logs?page_size=20").json()
        assert r20["page_size"] == 20 and len(r20["items"]) == 20
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_logs_page_out_of_range(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pid = _publish_three_node_pipeline(client)
        _add_run(app, pid, {"0": {"ok": 1}}, status="done")
        r = client.get(f"/api/pipelines/{pid}/logs?page=999&page_size=20").json()
        assert r["items"] == [] and r["total"] == 1
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_logs_bad_date_400(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pid = _publish_three_node_pipeline(client)
        assert client.get(f"/api/pipelines/{pid}/logs?start_date=2026-13-99").status_code == 400
    finally:
        app.cleanup()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest server/tests/test_pipeline_logs.py -q`
Expected: 现有 3 个端点测试 + 5 个新端点测试 FAIL（端点仍返回裸 list / 不认参数；`KeyError: 'items'`、400 未触发等）。Task 1 的两个 helper 测试与 3 个 `build_run_log_rows` 单测仍 PASS。

- [ ] **Step 3: 实现 `list_run_log_page`（+ 补 `func` import）**

在 `server/app/modules/pipelines/run_logs.py` 文件头加回 `from sqlalchemy import func`（最终文件头）：

```python
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func

from server.app.modules.pipelines.schemas import RunLogRow

_BEIJING_OFFSET = timedelta(hours=8)
```

在 `beijing_day_to_utc_range` 下方新增：

```python
def list_run_log_page(
    db, pipeline_id, *, page: int, page_size: int, start_dt, end_dt
) -> tuple[list[RunLogRow], int]:
    """按「日志行」服务端分页。返回 (当前页行, 满足筛选的总行数)。

    A 方案：SUM(JSON_LENGTH) 取精确总行数；游走 (id, 行数) 整型找到覆盖
    [offset, offset+page_size) 的 run，只对这些 run 加载 node_results 摊平后精确切片。
    """
    from server.app.modules.pipelines.models import PipelineNode, PipelineRun

    name_by_index = {
        n.node_index: n.name
        for n in db.query(PipelineNode).filter(PipelineNode.pipeline_id == pipeline_id).all()
    }
    time_col = func.coalesce(PipelineRun.completed_at, PipelineRun.created_at)
    rowcount_col = func.coalesce(func.json_length(PipelineRun.node_results), 0)

    base = db.query(PipelineRun).filter(PipelineRun.pipeline_id == pipeline_id)
    if start_dt is not None:
        base = base.filter(time_col >= start_dt)
    if end_dt is not None:
        base = base.filter(time_col < end_dt)

    total = int(base.with_entities(func.coalesce(func.sum(rowcount_col), 0)).scalar() or 0)
    offset = (page - 1) * page_size
    if offset >= total:
        return [], total

    id_counts = (
        base.with_entities(PipelineRun.id, rowcount_col)
        .order_by(time_col.desc(), PipelineRun.id.desc())
        .all()
    )
    cum = 0
    skip_in_first = 0
    window_ids: list[int] = []
    for run_id, cnt in id_counts:
        cnt = int(cnt)
        if cnt == 0:
            continue
        if cum + cnt <= offset:
            cum += cnt
            continue
        if not window_ids:
            skip_in_first = offset - cum
        window_ids.append(run_id)
        cum += cnt
        if cum >= offset + page_size:
            break

    if not window_ids:
        return [], total

    runs = {r.id: r for r in base.filter(PipelineRun.id.in_(window_ids)).all()}
    rows: list[RunLogRow] = []
    for rid in window_ids:  # 保持时间倒序
        rows.extend(build_run_log_rows(runs[rid], name_by_index))
    return rows[skip_in_first : skip_in_first + page_size], total
```

- [ ] **Step 4: 加 `RunLogPage` schema**

在 `server/app/modules/pipelines/schemas.py` 末尾（`RunLogRow` 之后）加：

```python
class RunLogPage(BaseModel):
    items: list[RunLogRow]
    total: int
    page: int
    page_size: int
```

- [ ] **Step 5: 重写端点 `list_run_logs`**

把 `server/app/modules/pipelines/router.py` 末尾的 `list_run_logs`（含其函数体）整体替换为：

```python
@router.get("/{pipeline_id}/logs")
def list_run_logs(
    pipeline_id: int,
    page: int = 1,
    page_size: int = 30,
    start_date: str | None = None,
    end_date: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from server.app.modules.pipelines.run_logs import (
        beijing_day_to_utc_range,
        list_run_log_page,
    )

    _owned(db, pipeline_id, user)
    page = max(1, page)
    page_size = page_size if page_size in (20, 30) else 30
    try:
        start_dt, end_dt = beijing_day_to_utc_range(start_date, end_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式应为 YYYY-MM-DD") from None

    rows, total = list_run_log_page(
        db, pipeline_id, page=page, page_size=page_size, start_dt=start_dt, end_dt=end_dt
    )
    return RunLogPage(items=rows, total=total, page=page, page_size=page_size).model_dump()
```

并把顶部 schema import 块里加上 `RunLogPage`（现有 import 块在 `router.py` 第 16-24 行）：

```python
from server.app.modules.pipelines.schemas import (
    DraftSave,
    PipelineCreate,
    PipelinePatch,
    PipelineRead,
    PublishRequest,
    RunLogPage,
    RunRead,
    VersionRead,
)
```

> 旧端点里 `from ... import PipelineNode, PipelineRun` 与 `from ... run_logs import build_run_log_rows` 的本地 import 随旧函数体一并删除（新函数体不再用它们）。

- [ ] **Step 6: Run all backend tests to verify they pass**

Run: `pytest server/tests/test_pipeline_logs.py -q`
Expected: 全部 PASS（3 个 `build_run_log_rows` 单测 + 2 个 helper 单测 + 3 个改造端点测试 + 5 个新端点测试）。

- [ ] **Step 7: Lint**

Run: `ruff check server/app/modules/pipelines/ server/tests/test_pipeline_logs.py` 然后 `ruff format --check server/app/modules/pipelines/`
Expected: 无错误（如 format 不符，去掉 `--check` 重跑改写后再 commit）。

- [ ] **Step 8: Commit**

```bash
git add server/app/modules/pipelines/run_logs.py server/app/modules/pipelines/schemas.py server/app/modules/pipelines/router.py server/tests/test_pipeline_logs.py
git commit -m "feat(pipelines): 日志端点 服务端按行分页 + 起止日期筛选（envelope）"
```

---

## Task 3: 前端类型 + API 客户端

**Files:**
- Modify: `web/src/types.ts`
- Modify: `web/src/api/pipelines.ts`

- [ ] **Step 1: 加 `RunLogPage` 类型**

在 `web/src/types.ts` 的 `RunLogRow` 类型定义（结尾 `};`）之后追加：

```ts
export type RunLogPage = {
  items: RunLogRow[];
  total: number;
  page: number;
  page_size: number;
};
```

- [ ] **Step 2: 改 `listPipelineLogs`**

在 `web/src/api/pipelines.ts`：把第 2 行 import 里的 `RunLogRow` 换成 `RunLogPage`（该文件不再直接用 `RunLogRow`）：

```ts
import type { NodeTypeDef, Pipeline, PipelineRun, PipelineVersionSummary, RunLogPage } from "../types";
```

把文件末尾的 `listPipelineLogs`（第 38-39 行）替换为：

```ts
export const listPipelineLogs = (
  id: number,
  opts: { page?: number; pageSize?: number; startDate?: string; endDate?: string } = {},
) => {
  const p = new URLSearchParams();
  p.set("page", String(opts.page ?? 1));
  p.set("page_size", String(opts.pageSize ?? 30));
  if (opts.startDate) p.set("start_date", opts.startDate);
  if (opts.endDate) p.set("end_date", opts.endDate);
  return api<RunLogPage>(`/api/pipelines/${id}/logs?${p.toString()}`);
};
```

- [ ] **Step 3: Typecheck（此时 AgentLogsView 仍用旧调用，会报错——预期，下一任务修）**

Run: `pnpm --filter @geo/web typecheck`
Expected: 仅 `AgentLogsView.tsx` 报错（`listPipelineLogs` 返回类型变了、`r` 不再是数组）。`types.ts` / `pipelines.ts` 本身无错。**本步先不 commit**，与 Task 4 合并提交（避免中间态 typecheck 红）。

---

## Task 4: `AgentLogsView` 加筛选栏 + 滚动容器 + 分页器（+ CSS）

**Files:**
- Modify: `web/src/features/pipelines/AgentLogsView.tsx`
- Modify: `web/src/styles.css`

- [ ] **Step 1: 重写 `AgentLogsView.tsx`**

整体替换 `web/src/features/pipelines/AgentLogsView.tsx` 为：

```tsx
// web/src/features/pipelines/AgentLogsView.tsx
import { useCallback, useEffect, useState } from "react";
import { getPipeline, listPipelineLogs } from "../../api/pipelines";
import { useToast } from "../../components/Toast";
import type { Pipeline, RunLogRow } from "../../types";

function fmtTime(t: string | null): string {
  if (!t) return "—";
  return new Date(t).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}

const ERR = { color: "#c0392b" };
const ERR_BOLD = { color: "#c0392b", fontWeight: 600 };

export function AgentLogsView({ pipelineId, onBack }:
  { pipelineId: number; onBack: () => void }) {
  const { toast } = useToast();
  const [rows, setRows] = useState<RunLogRow[]>([]);
  const [total, setTotal] = useState(0);
  const [agent, setAgent] = useState<Pipeline | null>(null);
  const [loading, setLoading] = useState(true);

  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(30);
  // 输入框值（编辑中）；点「筛选」后才落到 applied*，再用于请求
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [appliedStart, setAppliedStart] = useState("");
  const [appliedEnd, setAppliedEnd] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [a, r] = await Promise.all([
        getPipeline(pipelineId),
        listPipelineLogs(pipelineId, {
          page,
          pageSize,
          startDate: appliedStart || undefined,
          endDate: appliedEnd || undefined,
        }),
      ]);
      setAgent(a);
      setRows(r.items);
      setTotal(r.total);
    } catch (e) {
      toast(e instanceof Error ? e.message : "加载日志失败", "error");
    } finally {
      setLoading(false);
    }
  }, [pipelineId, page, pageSize, appliedStart, appliedEnd, toast]);
  useEffect(() => { load(); }, [load]);

  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  // total 变化后若当前页越界（如筛选缩小结果集），回退到最后一页
  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

  const isErrBatch = (s: string) => s === "failed" || s === "partial_failed";

  const applyFilter = () => {
    setAppliedStart(startDate);
    setAppliedEnd(endDate);
    setPage(1);
  };
  const resetFilter = () => {
    setStartDate("");
    setEndDate("");
    setAppliedStart("");
    setAppliedEnd("");
    setPage(1);
  };

  return (
    <div className="agentsWorkspace">
      <div className="topbar">
        <div>
          <p className="eyebrow">智能体 · 日志</p>
          <h1>{agent ? agent.name : `智能体 ${pipelineId}`}</h1>
        </div>
        <div className="agentRowActions">
          <button onClick={load}>刷新</button>
          <button onClick={onBack}>← 返回智能体列表</button>
        </div>
      </div>

      <div className="agentLogsFilter">
        <label>
          开始日期
          <input
            type="date"
            value={startDate}
            max={endDate || undefined}
            onChange={(e) => setStartDate(e.target.value)}
          />
        </label>
        <label>
          结束日期
          <input
            type="date"
            value={endDate}
            min={startDate || undefined}
            onChange={(e) => setEndDate(e.target.value)}
          />
        </label>
        <button onClick={applyFilter}>筛选</button>
        <button onClick={resetFilter}>重置</button>
      </div>

      <div className="agentLogsScroll">
        <table className="agentTable">
          <thead>
            <tr>
              <th>批次</th><th>任务名称</th><th>步骤</th><th>日志等级</th><th>日志</th><th>时间</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.batch}-${r.step}`}>
                <td style={isErrBatch(r.run_status) ? ERR : undefined}>{r.batch}</td>
                <td>{r.task_name}</td>
                <td>{r.step}</td>
                <td style={r.level === "ERROR" ? ERR_BOLD : undefined}>{r.level}</td>
                <td style={r.level === "ERROR" ? ERR : undefined}>{r.message}</td>
                <td>{fmtTime(r.time)}</td>
              </tr>
            ))}
            {!loading && rows.length === 0 && (
              <tr><td colSpan={6}><div className="agentEmpty">暂无运行日志</div></td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="agentLogsPager">
        <button disabled={page <= 1 || loading} onClick={() => setPage((p) => Math.max(1, p - 1))}>
          上一页
        </button>
        <span className="agentLogsPageInfo">第 {page} / {totalPages} 页 · 共 {total} 条</span>
        <button disabled={page >= totalPages || loading} onClick={() => setPage((p) => p + 1)}>
          下一页
        </button>
        <select
          value={pageSize}
          onChange={(e) => { setPageSize(Number(e.target.value)); setPage(1); }}
        >
          <option value={20}>20 条/页</option>
          <option value={30}>30 条/页</option>
        </select>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: 加 CSS**

在 `web/src/styles.css` 的 `.agentEmpty { ... }` 行（约 2658 行）之后追加：

```css
.agentLogsFilter {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.agentLogsFilter label {
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 11px;
  color: var(--fg-3);
  letter-spacing: .3px;
}
.agentLogsFilter input[type="date"] {
  height: 30px;
  padding: 0 9px;
  font-size: 13px;
  border: 1px solid var(--hair-2);
  border-radius: var(--r-sm);
  background: var(--paper);
  color: var(--fg-2);
}
.agentLogsFilter button {
  height: 30px;
  padding: 0 14px;
  font-size: 12px;
  border: 1px solid var(--hair-2);
  border-radius: var(--r-sm);
  color: var(--fg-2);
  background: var(--paper);
}
.agentLogsFilter button:hover { background: var(--cream); color: var(--fg); }

/* 固定高度 + 内部滚动；表头吸顶（须有不透明背景避免行穿透） */
.agentLogsScroll {
  max-height: 60vh;
  overflow-y: auto;
  border: 1px solid var(--hair);
  border-radius: var(--r-sm);
}
.agentLogsScroll thead th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--paper);
}

.agentLogsPager {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 14px;
}
.agentLogsPager button {
  height: 30px;
  padding: 0 14px;
  font-size: 12px;
  border: 1px solid var(--hair-2);
  border-radius: var(--r-sm);
  color: var(--fg-2);
  background: var(--paper);
}
.agentLogsPager button:hover:not(:disabled) { background: var(--cream); color: var(--fg); }
.agentLogsPager button:disabled { opacity: .45; cursor: not-allowed; }
.agentLogsPager .agentLogsPageInfo { font-size: 12px; color: var(--fg-3); }
.agentLogsPager select {
  height: 30px;
  padding: 0 8px;
  font-size: 12px;
  border: 1px solid var(--hair-2);
  border-radius: var(--r-sm);
  background: var(--paper);
  color: var(--fg-2);
  margin-left: auto;
}
```

- [ ] **Step 3: Typecheck + lint + build**

Run: `pnpm --filter @geo/web typecheck`
Expected: PASS（无错误）。

Run: `pnpm --filter @geo/web lint`
Expected: 无新增 error（该模块无 lint 报错）。

Run: `pnpm --filter @geo/web build`
Expected: 构建成功。

- [ ] **Step 4: Commit（前端整体一次提交）**

```bash
git add web/src/types.ts web/src/api/pipelines.ts web/src/features/pipelines/AgentLogsView.tsx web/src/styles.css
git commit -m "feat(web): 智能体日志页 起止日期筛选 + 滚动(表头吸顶) + 分页器"
```

---

## Task 5: 收尾验证

- [ ] **Step 1: 全量后端 pipelines 测试**

Run: `pytest server/tests/test_pipeline_logs.py -q`（如本地有完整 MySQL，可加跑相邻 `pytest server/tests/test_pipelines*.py -q`）
Expected: 全 PASS。

- [ ] **Step 2: 前端门禁**

Run: `pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 均成功。

- [ ] **Step 3: 对照验收标准自查**（见 spec §5）

逐条确认：① 顶部纯日期起止筛选、筛后回第 1 页；② 表格区固定高度内部滚动、表头吸顶；③ 分页器 `第 X / Y 页 · 共 N 条` + 边界禁用 + 20/30 切换回第 1 页；④ 按行翻页、跨批次边界连续不重不漏；⑤ 可翻任意早期历史；⑥ 着色/北京时间/空态与 #32 一致；⑦ 不改引擎、不加表/迁移、不动审计。

- [ ] **Step 4: 可选 — 手动联调**

起后端 `uvicorn server.app.main:app --reload --port 8000` + 前端 `pnpm --filter @geo/web dev`（5173），登录 → 智能体管理 → 某智能体「日志」，验证滚动 / 翻页 / 日期筛选实际表现。

---

## Self-Review（已核对）

- **Spec 覆盖**：分页(Task 2/4)、日期筛选(Task 1/2/4)、滚动吸顶(Task 4 CSS)、envelope(Task 2)、A 方案 JSON_LENGTH 游走(Task 2)、北京日 +08:00(Task 1)、页大小 20/30 默认 30(Task 2/4)、打开无筛选(Task 4 初值空)、测试改造+新增(Task 2) —— 全部有对应任务。
- **占位符**：无 TBD/TODO；每个代码步骤给出完整代码。
- **类型一致**：后端 `RunLogPage{items,total,page,page_size}` 与前端 `RunLogPage` 字段名一致；`listPipelineLogs(id, opts)` 签名在 Task 3 定义、Task 4 调用一致；`list_run_log_page(db, pid, *, page, page_size, start_dt, end_dt)` 定义(Task 2 Step3)与调用(Task 2 Step5)一致；`beijing_day_to_utc_range(start_date, end_date)` 定义(Task 1)与调用(Task 2 Step5)一致。
- **已知坑**：Task 1 Step3 故意不加 `from sqlalchemy import func`（否则 ruff F401），留到 Task 2 Step3 用到时再加——已在步骤中标注。
```
