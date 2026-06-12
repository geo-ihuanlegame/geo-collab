# 智能体「日志」功能 — 设计方案

- **日期**：2026-06-05
- **聚焦**：在「智能体管理」操作区新增 **日志** 入口，点开为全页次级页，展示某智能体历次运行的逐节点日志（批次 / 任务名称 / 步骤 / 日志等级 / 日志 / 时间）。
- **落地工程**：`geo-collab` 主仓库。参考截图来自参考系统，仅作视觉参照、不照搬其数据模型。
- **前置**：pipelines 引擎 + `PipelineRun`（含 `node_results`）+ 定时调度 + 智能体管理界面均已在 main。

> **整体愿景对齐**：智能体定时无人值守运行，运营每天只在内容管理审核。当一次自动运行没产出或报错时，运营/管理员需要一个**只读的运行日志页**快速看「哪一步、什么级别、什么消息、什么时候」——本特性提供这个排查入口。

---

## 1. 关键取舍（已与用户确认）

1. **轻量版：从现有 run 记录派生**，**不改执行引擎、不加表、不写迁移**。每次运行已存为一条 `PipelineRun`（`node_results` 按节点下标记录成功/跳过/报错，`status` + 时间戳）。日志页把这些 run 摊平成「一行一节点步骤」。
2. **全页次级页**：与现有「配置流程」（`PipelineEditor`）一致的全页切换 + 「← 返回智能体列表」，不用弹窗。
3. **消息通用文案**：成功→「运行成功」、跳过→「已跳过」、报错→错误文本；不为每种节点定制摘要（YAGNI）。
4. **「历史记录」按钮先省略**：本页本身即历史（最近 N 批，新→旧）。超过 N 再加「加载更多」。

### 非目标（YAGNI）
- 不加 `PipelineRunLog` 表、不改 `executor.py`、不做逐步独立时间戳（同一批次各步时间相同 = 该次运行时间）。
- 不做按等级/批次筛选、不做实时刷新/SSE（可选一个手动「刷新」按钮）。
- 不动审计日志模块（`/api/audit-logs`，admin-only，用途不同）。

---

## 2. 数据来源与字段映射

现有 `PipelineRun`（`server/app/modules/pipelines/models.py`）：
- `id`（→ 批次）、`pipeline_id`、`status`（pending/running/done/partial_failed/failed）
- `node_results: dict`，形如 `{"0": {...output}, "1": {"error": "..."}, "2": {"skipped": true}}`，**键为节点下标字符串**
- `created_at` / `completed_at`

节点名不在 run 内（`node_results` 只有下标）。名字取自该智能体**当前已发布节点**（`GET /api/pipelines/{id}` 返回的 `nodes[]`，含 `node_index` + `name`），按下标映射。

> **已知局限（可接受）**：若流程在某次运行后改过名/增删节点，旧批次的「任务名称」按当前节点名映射可能不准；缺失下标回退显示「步骤 N」。轻量只读日志可接受，spec 明示。

每个 run 摊平成日志行（按节点下标升序），字段：

| 字段 | 来源 / 规则 |
|---|---|
| `batch` 批次 | `run.id` |
| `run_status` | `run.status`（前端给批次着色：失败类标红） |
| `step` 步骤 | 节点下标（int） |
| `task_name` 任务名称 | 当前已发布节点名按下标映射；缺失 → `f"步骤 {step}"` |
| `level` 日志等级 | `node_results[idx]` 含 `error` 或非空 `errors` → `"ERROR"`；否则 `"INFO"` |
| `message` 日志 | `error` → 该错误文本；`errors`(list) → `"; ".join`；`skipped` 为真 → `"已跳过"`；否则 → `"运行成功"` |
| `time` 时间 | `run.completed_at`，缺则 `run.created_at`（ISO UTC，前端转北京时间显示） |

`status` 为 `pending`/`running` 的在途运行：其 `node_results` 可能为空 → 该批次无明细行；可显示一条「批次 N · 运行中」占位（v1 可省略，仅展示有明细的批次）。**决定：v1 只摊平已有 `node_results` 的节点行；某批次 `node_results` 为空则该批次不产生行**（运行中/刚触发的批次稍后刷新即可见）。

---

## 3. 后端

### 3.1 新增只读端点
`GET /api/pipelines/{pipeline_id}/logs?limit=50`（`server/app/modules/pipelines/router.py`）
- `_owned(db, pipeline_id, user)` 复用 owner/admin 校验。
- 读当前已发布节点建 `index→name` 映射。
- 取最近 `limit`（默认 50，1..200 夹紧）条 run（`order_by id desc`）。
- 对每条 run，遍历 `node_results`，**按 int(下标) 升序**摊平成 `RunLogRow` 列表（批次内保持步骤序；批次之间新→旧）。
- 返回 `list[RunLogRow]`（已扁平：批次间用 `batch` 区分，前端按 batch 分组渲染）。

### 3.2 schema（`server/app/modules/pipelines/schemas.py`）
```python
class RunLogRow(BaseModel):
    batch: int
    run_status: str
    step: int
    task_name: str
    level: str       # "INFO" | "ERROR"
    message: str
    time: datetime | None
```

### 3.3 摊平逻辑（router 内或抽到 service 函数 `build_run_log_rows`）
为可测试，抽一个纯函数：
```python
def build_run_log_rows(run, name_by_index: dict[int, str]) -> list[RunLogRow]:
    rows = []
    for key in sorted(run.node_results or {}, key=lambda k: int(k)):
        idx = int(key)
        data = run.node_results[key] or {}
        if "error" in data:
            level, message = "ERROR", str(data["error"])
        elif data.get("errors"):
            level, message = "ERROR", "; ".join(str(e) for e in data["errors"])
        elif data.get("skipped"):
            level, message = "INFO", "已跳过"
        else:
            level, message = "INFO", "运行成功"
        rows.append(RunLogRow(
            batch=run.id, run_status=run.status, step=idx,
            task_name=name_by_index.get(idx, f"步骤 {idx}"),
            level=level, message=message,
            time=run.completed_at or run.created_at,
        ))
    return rows
```

---

## 4. 前端

### 4.1 操作列加入口（`AgentManagementWorkspace.tsx`）
列表操作列在「配置流程 / 立即运行」旁加：
```tsx
<button onClick={() => setLogsId(p.id)}>日志</button>
```
新增 `const [logsId, setLogsId] = useState<number | null>(null);`。当 `logsId != null` 时，整页渲染 `<AgentLogsView pipelineId={logsId} onBack={() => setLogsId(null)} />`（与 `editingId` 渲染 `PipelineEditor` 同构，二者互斥：进入日志/编辑时另一个为 null）。

### 4.2 新组件 `AgentLogsView.tsx`（全页次级页）
- 顶部：`← 返回智能体列表` 按钮 + 智能体名标题 + 可选「刷新」按钮。
- 加载：`listPipelineLogs(pipelineId)` + `getPipeline(pipelineId)`（取智能体名做标题）。
- 渲染：一张表，列＝**批次 / 任务名称 / 步骤 / 日志等级 / 日志 / 时间**（对齐截图列序）。
  - 同一 `batch` 的行视觉归组（首行显示批次号，或每行都显示批次号——v1 每行都显示，简单）。
  - `level === "ERROR"` 行标红；`run_status` 为 `failed`/`partial_failed` 的批次号标红。
  - 时间按北京时间显示（复用项目已有时间格式化方式；无则 `toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })`）。
- 空态：无任何日志行 → 「暂无运行日志」。

### 4.3 API 客户端（`web/src/api/pipelines.ts` + `web/src/types.ts`）
```ts
export const listPipelineLogs = (id: number, limit = 50) =>
  api<RunLogRow[]>(`/api/pipelines/${id}/logs?limit=${limit}`);
```
`types.ts` 增 `RunLogRow` 类型（与后端 schema 对应）。

---

## 5. 测试

### 后端（`@pytest.mark.mysql`）
新增 `server/tests/test_pipeline_logs.py`：
1. **摊平正确**：造一个 pipeline + 已发布节点（如 3 个：问题源/AI创作/进入未审核库），手动插入一条 `PipelineRun`，`node_results = {"0": {"question_count": 3}, "1": {"errors": ["模板X无效"]}, "2": {"skipped": True}}`，`status="partial_failed"`。`GET /logs` →
   - 3 行，step 0/1/2，按序；
   - step0 level INFO message「运行成功」、task_name = 节点0名；
   - step1 level ERROR message 含「模板X无效」；
   - step2 level INFO message「已跳过」；
   - 每行 batch = run.id、run_status = "partial_failed"。
2. **多批次倒序**：两条 run（id 升序），返回中较新批次的行排在前。
3. **owner 校验**：另一个非 admin 用户访问 → **404**（`_owned` 对不属于自己的 pipeline 统一抛 `HTTPException(404, "工作流不存在")`）。
4. **空**：无 run → `[]`。
5. **节点改名局限**：node_results 含下标 5 但当前节点只有 0..2 → 该行 task_name = 「步骤 5」（不崩）。

（纯函数 `build_run_log_rows` 也可单测，但经 HTTP 端点测已覆盖。）

### 前端
`pnpm --filter @geo/web typecheck && build`。可选 Playwright：登录 → 智能体管理 → 某智能体「日志」→ 断言表头/行渲染。

---

## 6. 验收标准
1. 智能体列表操作列出现「日志」按钮；点击整页切换到该智能体日志页，「← 返回」可回列表（与「配置流程」交互一致）。
2. 日志页表格列＝批次/任务名称/步骤/日志等级/日志/时间；ERROR 行与失败批次标红；时间为北京时间。
3. 行数据正确：成功→INFO「运行成功」、跳过→INFO「已跳过」、报错→ERROR+错误文本；批次新→旧、批次内步骤升序。
4. 无运行 → 「暂无运行日志」。
5. 不改执行引擎、不加表/迁移、不动审计模块；后端端点测 + 前端 typecheck/build 全绿。

## 7. 风险与缓解
- **节点名映射不准（流程改过）**：明示为已知局限；缺失下标回退「步骤 N」，不崩。
- **node_results 结构演进**：摊平逻辑只依赖 `error`/`errors`/`skipped` 三个判定键 + 兜底「运行成功」，新增 output 键不影响。
- **大量历史 run**：`limit` 默认 50、上限 200 夹紧；超出再迭代「加载更多」。
- **在途批次无明细**：v1 仅摊平有 `node_results` 的节点；运行中批次稍后刷新可见（可选「刷新」按钮）。
