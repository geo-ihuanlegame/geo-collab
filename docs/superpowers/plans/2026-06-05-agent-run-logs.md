# 智能体「日志」功能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 在「智能体管理」操作列加「日志」入口，点开为全页次级页，展示该智能体历次运行的逐节点日志（批次/任务名称/步骤/等级/消息/时间），数据从现有 `PipelineRun.node_results` 派生。

**Architecture:** 纯读侧派生，不改执行引擎、不加表、不写迁移。新增一个无 DB 依赖的纯函数把单条 run 摊平成日志行；新增只读端点 `GET /api/pipelines/{id}/logs`；前端新增全页 `AgentLogsView`（与「配置流程」同构），操作列加「日志」按钮。

**Tech Stack:** FastAPI + SQLAlchemy + MySQL + pytest（容器跑）；React 19 + Vite + TS（host pnpm）。

---

## 约定

- **唯一改动目标 = geo-collab**；参考项目只读禁改。
- **后端在容器**：`docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest <args>'`。宿主无 python。`server/` bind-mount。容器内若无 ruff：`pip install ruff -q`。
- **ruff 双门禁**：`ruff check server/` + `ruff format --check server/`。测试 import 放函数内或顶部按现有文件风格（本仓库 pipeline 测试惯例：import 放测试函数体内，见 `test_pipeline_review_distribute.py`）。
- **前端在 host**：`pnpm --filter @geo/web typecheck` + `pnpm --filter @geo/web build`。
- **分支** `feat/agent-run-logs`（已基于最新 `origin/main` 建好，spec 已提交 48d7e9f）。逐 Task 提交。
- 已核实事实（基于本分支 origin/main）：
  - `PipelineRun{id,pipeline_id,user_id,status,node_results(JSON,键为节点下标字符串),article_ids,error_message,created_at,completed_at}`（`pipelines/models.py`）。
  - `PipelineNode{pipeline_id,node_type,name,node_index,config,flow_meta}` 存当前已发布节点（publish 时写入；`GET /api/pipelines/{id}` 的 `nodes[]` 即来自它）。
  - `pipelines/router.py`：已 `from ...schemas import (... RunRead ...)`，已有 `_owned(db, pipeline_id, user)`（pipeline 不存在或非 owner 非 admin → `HTTPException(404,"工作流不存在")`），`get_run` 在 `/runs/{run_id}` 之后是文件较末尾；新端点加在 `get_run` 之后。
  - `pipelines/schemas.py`：顶部 `from datetime import datetime, time`，`from pydantic import BaseModel, ConfigDict`。
  - `pipelines/service.py` **不** import schemas（保持 service 无 schema 依赖）→ 纯函数放新模块 `run_logs.py`，不放 service。
  - 前端：`AgentManagementWorkspace.tsx` 用 `editingId` 状态切到全页 `PipelineEditor`；操作列在 `[149-152]` 行（编辑/配置流程/立即运行/删除）。`api/pipelines.ts` 顶部 `import type { NodeTypeDef, Pipeline, PipelineRun, PipelineVersionSummary } from "../types";`。`components/Toast` 提供 `useToast`。复用样式类：`agentsWorkspace / topbar / eyebrow / agentTable / agentRowActions / agentEmpty`（均已存在）。
  - 测试 app：`build_test_app(monkeypatch)` → `TestApp{client, session_factory, data_dir, ...}`，内置 admin 用户 `testadmin` + 已登录 cookie。发布流程：`POST /api/pipelines` → `POST /api/pipelines/{id}/draft {"snapshot":...}` → `POST /api/pipelines/{id}/publish {}`（见 `test_pipeline_review_distribute.py`）。

---

## File Structure

- `server/app/modules/pipelines/schemas.py` — 加 `RunLogRow`（响应模型）。
- `server/app/modules/pipelines/run_logs.py`（**新**）— 纯函数 `build_run_log_rows(run, name_by_index) -> list[RunLogRow]`，无 DB 依赖。
- `server/app/modules/pipelines/router.py` — 加端点 `GET /{pipeline_id}/logs`。
- `server/tests/test_pipeline_logs.py`（**新**）— 纯函数单测 + 端点集成测。
- `web/src/types.ts` — 加 `RunLogRow` 类型。
- `web/src/api/pipelines.ts` — 加 `listPipelineLogs`。
- `web/src/features/pipelines/AgentLogsView.tsx`（**新**）— 全页日志视图。
- `web/src/features/pipelines/AgentManagementWorkspace.tsx` — `logsId` 状态 + 「日志」按钮 + 渲染 `AgentLogsView`。

---

## Task 1: 后端纯函数 `build_run_log_rows` + schema

**Files:**
- Modify: `server/app/modules/pipelines/schemas.py`
- Create: `server/app/modules/pipelines/run_logs.py`
- Test: `server/tests/test_pipeline_logs.py`（新建）

- [ ] **Step 1: 写失败测试（纯函数，无 DB → 不需要 @pytest.mark.mysql）**

```python
# server/tests/test_pipeline_logs.py
from datetime import datetime
from types import SimpleNamespace


def test_build_run_log_rows_levels_and_order():
    from server.app.modules.pipelines.run_logs import build_run_log_rows

    run = SimpleNamespace(
        id=7,
        status="partial_failed",
        node_results={
            "2": {"skipped": True},
            "0": {"question_count": 3},
            "1": {"errors": ["X无效"]},
        },
        completed_at=datetime(2026, 6, 5, 8, 0, 0),
        created_at=datetime(2026, 6, 5, 7, 0, 0),
    )
    names = {0: "问题源", 1: "AI创作", 2: "进入未审核库"}
    rows = build_run_log_rows(run, names)
    assert [r.step for r in rows] == [0, 1, 2]  # 按下标升序
    assert rows[0].level == "INFO" and rows[0].message == "运行成功" and rows[0].task_name == "问题源"
    assert rows[1].level == "ERROR" and "X无效" in rows[1].message
    assert rows[2].level == "INFO" and rows[2].message == "已跳过"
    assert all(r.batch == 7 and r.run_status == "partial_failed" for r in rows)
    assert rows[0].time == datetime(2026, 6, 5, 8, 0, 0)  # 优先 completed_at


def test_build_run_log_rows_error_fallback_name_and_time():
    from server.app.modules.pipelines.run_logs import build_run_log_rows

    run = SimpleNamespace(
        id=9,
        status="failed",
        node_results={"0": {"error": "boom"}, "5": {"foo": 1}},
        completed_at=None,
        created_at=datetime(2026, 6, 5, 7, 0, 0),
    )
    rows = build_run_log_rows(run, {0: "问题源"})  # 下标 5 无对应节点名
    assert rows[0].level == "ERROR" and rows[0].message == "boom"
    assert rows[1].task_name == "步骤 5"  # 回退
    assert rows[1].message == "运行成功"  # 非错误/跳过 → 兜底
    assert rows[0].time == datetime(2026, 6, 5, 7, 0, 0)  # completed_at 缺 → created_at


def test_build_run_log_rows_empty():
    from server.app.modules.pipelines.run_logs import build_run_log_rows

    run = SimpleNamespace(
        id=1, status="running", node_results={}, completed_at=None, created_at=datetime(2026, 6, 5, 7, 0, 0)
    )
    assert build_run_log_rows(run, {}) == []
```

- [ ] **Step 2: 运行确认失败**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_pipeline_logs.py -q -k build_run_log_rows'`
Expected: FAIL（ModuleNotFoundError: run_logs）

- [ ] **Step 3: 加 `RunLogRow` schema**

`schemas.py` 末尾追加（`datetime` 已 import）：
```python
class RunLogRow(BaseModel):
    batch: int
    run_status: str
    step: int
    task_name: str
    level: str  # "INFO" | "ERROR"
    message: str
    time: datetime | None = None
```

- [ ] **Step 4: 实现纯函数**

```python
# server/app/modules/pipelines/run_logs.py
from __future__ import annotations

from server.app.modules.pipelines.schemas import RunLogRow


def build_run_log_rows(run, name_by_index: dict[int, str]) -> list[RunLogRow]:
    """把单条 PipelineRun 的 node_results 摊平成日志行（按节点下标升序）。

    run 需具备 id / status / node_results / completed_at / created_at 属性。
    纯函数、无 DB 依赖，便于单测。
    """
    rows: list[RunLogRow] = []
    results = run.node_results or {}
    for key in sorted(results, key=lambda k: int(k)):
        idx = int(key)
        data = results[key] or {}
        if "error" in data:
            level, message = "ERROR", str(data["error"])
        elif data.get("errors"):
            level, message = "ERROR", "; ".join(str(e) for e in data["errors"])
        elif data.get("skipped"):
            level, message = "INFO", "已跳过"
        else:
            level, message = "INFO", "运行成功"
        rows.append(
            RunLogRow(
                batch=run.id,
                run_status=run.status,
                step=idx,
                task_name=name_by_index.get(idx, f"步骤 {idx}"),
                level=level,
                message=message,
                time=run.completed_at or run.created_at,
            )
        )
    return rows
```

- [ ] **Step 5: 运行通过 + ruff**

Run:
```
docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_pipeline_logs.py -q -k build_run_log_rows && pip install ruff -q 2>/dev/null; ruff check server/app/modules/pipelines/run_logs.py server/app/modules/pipelines/schemas.py server/tests/test_pipeline_logs.py && ruff format --check server/app/modules/pipelines/run_logs.py server/app/modules/pipelines/schemas.py server/tests/test_pipeline_logs.py'
```
Expected: 3 passed + ruff clean。

- [ ] **Step 6: 提交**

```bash
git add server/app/modules/pipelines/schemas.py server/app/modules/pipelines/run_logs.py server/tests/test_pipeline_logs.py
git commit -m "feat(pipelines): build_run_log_rows 纯函数 + RunLogRow schema"
```

---

## Task 2: 后端端点 `GET /api/pipelines/{id}/logs`

**Files:**
- Modify: `server/app/modules/pipelines/router.py`
- Test: `server/tests/test_pipeline_logs.py`（追加）

- [ ] **Step 1: 追加失败测试（@pytest.mark.mysql）**

```python
import pytest

from server.tests.utils import build_test_app


def _publish_three_node_pipeline(client, name="日志测试"):
    pid = client.post("/api/pipelines", json={"name": name, "type": "generation"}).json()["id"]
    snapshot = {
        "schemaVersion": 1,
        "nodes": [
            {"node_type": "question_source", "name": "问题源", "node_index": 0, "config": {}, "flow_meta": None},
            {"node_type": "ai_compose", "name": "AI创作", "node_index": 1, "config": {}, "flow_meta": None},
            {"node_type": "to_review", "name": "进入未审核库", "node_index": 2, "config": {}, "flow_meta": None},
        ],
    }
    client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snapshot})
    client.post(f"/api/pipelines/{pid}/publish", json={})
    return pid


def _add_run(app, pid, node_results, status="partial_failed"):
    from server.app.modules.pipelines.models import PipelineRun
    from server.app.modules.system.models import User

    with app.session_factory() as db:
        uid = db.query(User).first().id
        run = PipelineRun(
            pipeline_id=pid, user_id=uid, status=status, node_results=node_results, article_ids=[]
        )
        db.add(run)
        db.commit()
        return run.id


@pytest.mark.mysql
def test_logs_endpoint_flattens_run(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pid = _publish_three_node_pipeline(client)
        _add_run(
            app,
            pid,
            {"0": {"question_count": 3}, "1": {"errors": ["模板X无效"]}, "2": {"skipped": True}},
        )
        rows = client.get(f"/api/pipelines/{pid}/logs").json()
        assert [r["step"] for r in rows] == [0, 1, 2]
        assert rows[0]["task_name"] == "问题源"
        assert rows[0]["level"] == "INFO" and rows[0]["message"] == "运行成功"
        assert rows[1]["level"] == "ERROR" and "模板X无效" in rows[1]["message"]
        assert rows[2]["message"] == "已跳过"
        assert rows[0]["run_status"] == "partial_failed"
        assert all(r["batch"] for r in rows)
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_logs_newest_batch_first(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pid = _publish_three_node_pipeline(client)
        first = _add_run(app, pid, {"0": {"ok": 1}}, status="done")
        second = _add_run(app, pid, {"0": {"ok": 1}}, status="done")
        assert second > first
        rows = client.get(f"/api/pipelines/{pid}/logs").json()
        # 较新批次的行在前
        assert rows[0]["batch"] == second
        assert rows[-1]["batch"] == first
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_logs_empty_and_not_found(monkeypatch):
    app = build_test_app(monkeypatch)
    client = app.client
    try:
        pid = client.post("/api/pipelines", json={"name": "空日志", "type": "general"}).json()["id"]
        assert client.get(f"/api/pipelines/{pid}/logs").json() == []  # 无 run
        assert client.get("/api/pipelines/999999/logs").status_code == 404  # _owned 守卫
    finally:
        app.cleanup()
```

- [ ] **Step 2: 运行确认失败**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_pipeline_logs.py -q -k "endpoint or newest or empty"'`
Expected: FAIL（404，端点不存在）

- [ ] **Step 3: 加端点**

`router.py` 在 `get_run`（`/runs/{run_id}`）函数之后追加：
```python
@router.get("/{pipeline_id}/logs")
def list_run_logs(
    pipeline_id: int,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from server.app.modules.pipelines.models import PipelineNode, PipelineRun
    from server.app.modules.pipelines.run_logs import build_run_log_rows

    _owned(db, pipeline_id, user)
    limit = max(1, min(limit, 200))
    name_by_index = {
        n.node_index: n.name
        for n in db.query(PipelineNode).filter(PipelineNode.pipeline_id == pipeline_id).all()
    }
    runs = (
        db.query(PipelineRun)
        .filter(PipelineRun.pipeline_id == pipeline_id)
        .order_by(PipelineRun.id.desc())
        .limit(limit)
        .all()
    )
    rows: list[dict] = []
    for run in runs:
        rows.extend(r.model_dump() for r in build_run_log_rows(run, name_by_index))
    return rows
```
> 路由顺序：放在 `get_run` 之后即可。`/{pipeline_id}/logs` 与既有 `/{pipeline_id}/runs`、`/{pipeline_id}/versions` 同形，FastAPI 按声明匹配，无冲突。

- [ ] **Step 4: 运行通过 + ruff + 回归**

Run:
```
docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_pipeline_logs.py server/tests/test_pipeline_router.py -q && pip install ruff -q 2>/dev/null; ruff check server/app/modules/pipelines/router.py server/tests/test_pipeline_logs.py && ruff format --check server/app/modules/pipelines/router.py server/tests/test_pipeline_logs.py'
```
Expected: 全 PASS（含 6 个日志用例 + router 回归）+ ruff clean。

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/pipelines/router.py server/tests/test_pipeline_logs.py
git commit -m "feat(pipelines): GET /pipelines/{id}/logs 端点（派生逐节点运行日志）"
```

---

## Task 3: 前端类型 + API 客户端

**Files:**
- Modify: `web/src/types.ts`
- Modify: `web/src/api/pipelines.ts`

- [ ] **Step 1: 加类型**

`types.ts` 末尾追加：
```ts
export type RunLogRow = {
  batch: number;
  run_status: string;
  step: number;
  task_name: string;
  level: "INFO" | "ERROR";
  message: string;
  time: string | null;
};
```

- [ ] **Step 2: 加 API 客户端**

`api/pipelines.ts`：把顶部 import 改为带上 `RunLogRow`：
```ts
import type { NodeTypeDef, Pipeline, PipelineRun, PipelineVersionSummary, RunLogRow } from "../types";
```
文件末尾追加：
```ts
export const listPipelineLogs = (id: number, limit = 50) =>
  api<RunLogRow[]>(`/api/pipelines/${id}/logs?limit=${limit}`);
```

- [ ] **Step 3: typecheck**

Run（host）：`pnpm --filter @geo/web typecheck`
Expected: 通过。

- [ ] **Step 4: 提交**

```bash
git add web/src/types.ts web/src/api/pipelines.ts
git commit -m "feat(web): RunLogRow 类型 + listPipelineLogs 客户端"
```

---

## Task 4: 前端 `AgentLogsView` 全页组件

**Files:**
- Create: `web/src/features/pipelines/AgentLogsView.tsx`

- [ ] **Step 1: 新建组件**

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
  const [agent, setAgent] = useState<Pipeline | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [a, r] = await Promise.all([getPipeline(pipelineId), listPipelineLogs(pipelineId)]);
      setAgent(a);
      setRows(r);
    } catch (e) {
      toast(e instanceof Error ? e.message : "加载日志失败", "error");
    } finally {
      setLoading(false);
    }
  }, [pipelineId, toast]);
  useEffect(() => { load(); }, [load]);

  const isErrBatch = (s: string) => s === "failed" || s === "partial_failed";

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

      <table className="agentTable">
        <thead>
          <tr>
            <th>批次</th><th>任务名称</th><th>步骤</th><th>日志等级</th><th>日志</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.batch}-${r.step}-${i}`}>
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
  );
}
```

- [ ] **Step 2: typecheck + build**

Run（host）：`pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 通过（组件已被 import 才会进 build；本步先 typecheck 保证类型正确，build 在 Task 5 接线后再验完整）。
> 若 build 因「未被引用」tree-shake 不报错亦可；真正接线在 Task 5。

- [ ] **Step 3: 提交**

```bash
git add web/src/features/pipelines/AgentLogsView.tsx
git commit -m "feat(web): AgentLogsView 全页运行日志视图"
```

---

## Task 5: 接线 — 操作列「日志」按钮

**Files:**
- Modify: `web/src/features/pipelines/AgentManagementWorkspace.tsx`

- [ ] **Step 1: import 组件**

把顶部 `import { PipelineEditor } from "./PipelineEditor";` 之后加：
```tsx
import { AgentLogsView } from "./AgentLogsView";
```

- [ ] **Step 2: 加状态**

在 `const [editingId, setEditingId] = useState<number | null>(null);` 之后加：
```tsx
  const [logsId, setLogsId] = useState<number | null>(null);
```

- [ ] **Step 3: 加全页渲染分支**

在现有 `if (editingId != null) { ... }` 整块之后、`return (`（列表）之前，加：
```tsx
  if (logsId != null) {
    return <AgentLogsView pipelineId={logsId} onBack={() => { setLogsId(null); reload(); }} />;
  }
```

- [ ] **Step 4: 操作列加按钮**

把操作列改为在「立即运行」与「删除」之间插入「日志」：
```tsx
                  <button onClick={() => openEdit(p)}>编辑</button>
                  <button onClick={() => setEditingId(p.id)}>配置流程</button>
                  <button onClick={() => runNow(p)}>立即运行</button>
                  <button onClick={() => setLogsId(p.id)}>日志</button>
                  <button className="danger" onClick={() => remove(p)}>删除</button>
```

- [ ] **Step 5: typecheck + build**

Run（host）：`pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 通过。

- [ ] **Step 6: 提交**

```bash
git add web/src/features/pipelines/AgentManagementWorkspace.tsx
git commit -m "feat(web): 智能体操作列「日志」入口 + 接入 AgentLogsView"
```

---

## Task 6: 端到端验证（Playwright，可选但推荐）

**Files:** 无（仅验证）

- [ ] **Step 1: 后端全量 + 前端构建复核**

Run:
```
docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_pipeline_logs.py -q'
```
host: `pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 全绿。

- [ ] **Step 2: 浏览器验证（若 5173/8000 在跑）**

登录 → 智能体管理 → 任一智能体点「日志」→ 断言：整页切换、表头为 批次/任务名称/步骤/日志等级/日志/时间、「← 返回智能体列表」可回列表、无运行时显示「暂无运行日志」。
> 宿主无 playwright 包时此步可跳过，以 Task 2 的端点测 + Task 5 的 typecheck/build 为准。

---

## Self-Review 结果

- **Spec 覆盖**：§2 字段映射→Task1（build_run_log_rows 的 level/message/time/task_name 规则一致）；§3.1 端点+limit 夹紧+owner→Task2；§3.2 schema→Task1；§4.1 操作列入口+logsId→Task5；§4.2 AgentLogsView→Task4；§4.3 类型+客户端→Task3；§5 测试→Task1（纯函数3例）+Task2（端点3例：摊平/倒序/空+404）；§6 验收 1-5→Task5 接线 + Task4 渲染（空态/标红/北京时间）+ Task2（数据正确/倒序）。
- **占位符**：无 TBD；每步完整代码；唯一"先确认"项＝Task2 publish 流程字段（已据 test_pipeline_review_distribute.py 写实）。
- **类型一致**：`RunLogRow{batch,run_status,step,task_name,level,message,time}` 后端（schemas.py，Task1）与前端（types.ts，Task3）字段名/类型一致；`build_run_log_rows(run, name_by_index)` 签名跨 Task1/Task2 一致；端点 `model_dump()` 输出 dict 与前端 `RunLogRow` 字段对齐；`listPipelineLogs` 返回 `RunLogRow[]` 与 AgentLogsView 的 `rows` 一致。
- **owner 校验**：端点首行 `_owned` 抛 404；测试用「不存在 pipeline → 404」覆盖该守卫（跨用户为同一 `_owned` 共享逻辑，已在既有 run 端点覆盖，不重复造第二用户）。
- **不改引擎/不加表**：仅读 PipelineRun/PipelineNode + 纯函数 + 新端点 + 前端；无迁移、无 executor 改动。
