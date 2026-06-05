# 智能体「日志」分页 + 滚动 + 日期筛选 — 设计方案

- **日期**：2026-06-05
- **聚焦**：在已上线的「智能体 · 日志」全页视图（`AgentLogsView`）上加三样东西——**页内滚动条**（带固定表头）、**翻页**（每页 20/30 条日志行，默认 30）、**顶部起止日期筛选**（只按日期、不看时间）。
- **落地工程**：`geo-collab` 主仓库。
- **前置**：智能体日志特性（#32，`GET /api/pipelines/{id}/logs` 从 `PipelineRun.node_results` 派生逐节点日志行）已在 main。本方案是它的增量。
- **参考实现**：`web/src/features/system/AuditLogsWorkspace.tsx`（已有的日期筛选 + 服务端分页 + 滚动容器范式）与 `server/app/modules/audit/service.py`（日期/游标分页范式）。

> **愿景对齐**：智能体定时无人值守跑，运营靠这个只读日志页排查「哪一步、什么级别、什么消息、什么时候」。历史越攒越多，必须能往回翻、能按某天定位、单页又不至于把整页撑爆——本方案补齐这三点。

---

## 1. 关键决策（已与用户确认）

1. **分页单位＝日志行（每个节点步骤一行）**，每页 20/30 行，默认 30。同一次运行（批次）的多步会被算作多行，**翻页边界可能把一个批次切到两页**——用户已接受。
2. **历史深度＝全部，服务端分页**。不像 #32 那样一次性拉最近 50 批；改成 `page` / `page_size` 服务端切片，可翻到任意早期记录。
3. **日期筛选＝起止日期范围**（两个 `<input type="date">`，无时间分量）。
4. **后端分页算法＝A 方案（`JSON_LENGTH` 累加游走）**：用 SQL 聚合拿精确总行数，翻页时只加载落在当前页窗口内那几条 run 的 `node_results` JSON。拒绝 B 方案（每次请求把过滤集全量 JSON 拉出来在 Python 里摊平再切片）——在「无日期筛选 + 历史很大」时 B 会很慢。
5. **页大小选择器保留 20/30**（默认 30），不加 50。
6. **打开页面时无日期筛选**：默认展示全部历史的第 1 页（最新在前），与今天的行为一致。

### 非目标（YAGNI）

- 不加 `PipelineRunLog` 表、不改执行引擎 `executor.py`、不写迁移——仍从 `PipelineRun` 派生。
- 不做逐步独立时间戳（同批次各步时间相同＝该次运行时间）。
- 不做按日志等级 / 批次状态筛选，不做实时刷新 / SSE（保留手动「刷新」按钮）。
- 不动审计日志模块（`/api/audit-logs`，admin-only，用途不同）。

---

## 2. 后端

### 2.1 端点签名（`server/app/modules/pipelines/router.py`）

`GET /api/pipelines/{pipeline_id}/logs`，新增查询参数：

| 参数 | 类型 | 默认 | 约束 |
|---|---|---|---|
| `page` | int | 1 | `max(1, page)` |
| `page_size` | int | 30 | 夹紧到 `{20, 30}`（非 20/30 一律归 30） |
| `start_date` | `str \| None`（`YYYY-MM-DD`） | None | 解析失败 → 400 |
| `end_date` | `str \| None`（`YYYY-MM-DD`） | None | 解析失败 → 400；`end_date < start_date` → 空结果（不报错） |

> 旧的 `limit` 参数移除（#32 引入、仅本特性内部使用，无其它调用方）。`_owned(db, pipeline_id, user)` owner/admin 守卫不变（越权 → 404）。

### 2.2 响应 envelope（**破坏性变更**：原来返回裸 list）

```python
class RunLogPage(BaseModel):
    items: list[RunLogRow]   # 当前页的日志行（批次间新→旧，批次内步骤升序）
    total: int               # 满足筛选的总日志行数（用于算总页数）
    page: int
    page_size: int
```

`RunLogRow`（#32 已有，不变）：`batch / run_status / step / task_name / level / message / time`。

> 现有 3 个端点测试把 `.json()` 当裸 list 读，需改成读 `["items"]`——见 §4。改 envelope 不是为了好看，是分页必须回传 `total` 才能在前端算页码。

### 2.3 日期 → UTC 区间（Beijing 日历日）

`PipelineRun.created_at` / `completed_at` 用 `utcnow()` 存的是**朴素 UTC**。用户按「日期」筛选指的是北京自然日。固定 +08:00 换算（产品本就 China-only，`fmtTime` 已硬编码 `Asia/Shanghai`）：

- `start_date = "2026-06-05"` → 北京 `2026-06-05 00:00` → UTC `2026-06-04 16:00`（`= 北京零点 - 8h`）。
- `end_date = "2026-06-05"` → **次日**北京零点为开区间上界 → UTC `2026-06-05 16:00`，用 `< end_exclusive`。
- 实现：`datetime(Y,M,D) - timedelta(hours=8)`，`end` 用 `(end_date + 1 day) 的北京零点 - 8h`。

**筛选列＝`COALESCE(completed_at, created_at)`**，与 `time` 列展示的取值一致（「按我看到的那个日期筛」）。

### 2.4 分页算法（A 方案，service 新函数 `list_run_log_page`）

放在 `server/app/modules/pipelines/run_logs.py`（与 `build_run_log_rows` 同文件，复用它）：

```python
from sqlalchemy import func
from server.app.modules.pipelines.models import PipelineNode, PipelineRun

def list_run_log_page(db, pipeline_id, *, page, page_size, start_dt, end_dt):
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
        base = base.filter(time_col < end_dt)          # 开区间上界

    total = base.with_entities(func.coalesce(func.sum(rowcount_col), 0)).scalar() or 0
    offset = (page - 1) * page_size
    if offset >= total:
        return [], int(total)                          # 越界页 → 空

    # 只读 (id, 行数) 整型，按时间倒序游走，找到覆盖 [offset, offset+page_size) 的 run
    id_counts = (
        base.with_entities(PipelineRun.id, rowcount_col)
        .order_by(time_col.desc(), PipelineRun.id.desc())
        .all()
    )
    cum, skip_in_first, window_ids = 0, 0, []
    for run_id, cnt in id_counts:
        cnt = int(cnt)
        if cnt == 0:
            continue                                   # 空 node_results 不产生行
        if cum + cnt <= offset:
            cum += cnt
            continue
        if not window_ids:
            skip_in_first = offset - cum               # 首个窗口 run 里要丢弃的前导行数
        window_ids.append(run_id)
        cum += cnt
        if cum >= offset + page_size:
            break

    runs = {r.id: r for r in base.filter(PipelineRun.id.in_(window_ids)).all()}
    rows = []
    for rid in window_ids:                             # 保持时间倒序
        rows.extend(build_run_log_rows(runs[rid], name_by_index))
    return rows[skip_in_first: skip_in_first + page_size], int(total)
```

要点：
- `total` 用一条 SQL 聚合（`SUM(JSON_LENGTH)`）拿精确总行数——`build_run_log_rows` 对每个 key 恰好产 1 行，`JSON_LENGTH({})=0`，计数与摊平**完全一致**。
- `id_counts` 只取整型 `(id, 行数)`，且**命中窗口即 `break`**：第 1 页只游走 1~2 条；深页多走些整型行，依旧很轻。
- 只对窗口内的 run 加载 `node_results` JSON 并摊平，再按 `skip_in_first` 精确切到 `page_size` 行。
- 排序键 `time_col desc, id desc`：跨批次新→旧，批次内由 `build_run_log_rows` 保证步骤升序。

> **router 改动**：`list_run_logs` 解析 `start_date`/`end_date`→`datetime`（失败抛 `HTTPException(400)`），归一化 `page_size∈{20,30}`、`page=max(1,page)`，调 `list_run_log_page`，按 `RunLogPage` 返回。

---

## 3. 前端

### 3.1 API 客户端 + 类型（`web/src/api/pipelines.ts`、`web/src/types.ts`）

```ts
// types.ts
export type RunLogPage = { items: RunLogRow[]; total: number; page: number; page_size: number };

// pipelines.ts
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

### 3.2 `AgentLogsView.tsx` 改造

状态：`page`、`pageSize`(默认 30)、`startDate`/`endDate`（输入框）、`appliedStart`/`appliedEnd`（点「筛选」后才落地、用于请求，仿审计日志的 `filters` vs `appliedFilters`）、`rows`、`total`、`loading`。

- `load()` 依赖 `page`/`pageSize`/`appliedStart`/`appliedEnd`，调 `listPipelineLogs(pipelineId, {...})`，`setRows(items)`、`setTotal(total)`。
- 顶部 **筛选栏**（topbar 下方）：`开始日期`、`结束日期` 两个 `<input type="date">` + `筛选`（落地 applied* 并 `setPage(1)`）+ `重置`（清空、回第 1 页）。`刷新` / `← 返回` 保留。
- **滚动容器**：`<table>` 外包一层 `div`，固定 `maxHeight: 60vh` + `overflowY: auto`；`thead` 用 `position: sticky; top: 0`（配背景色避免行穿透表头）。这就是「滚动条看更早内容」——满页 30 行内部滚动，筛选栏与分页器不动。
- **分页器**（容器下方，常驻）：`上一页` / `下一页`（边界禁用）+ 文案 `第 {page} / {totalPages} 页 · 共 {total} 条` + 页大小 `<select>`（20 / 30，默认 30；切换时 `setPage(1)`）。`totalPages = Math.max(1, Math.ceil(total / pageSize))`；`page > totalPages` 时钳到 `totalPages`。
- 空态：`total === 0` → 「暂无运行日志」（沿用）。
- 着色逻辑不变：`level==="ERROR"` 行标红、失败类批次号标红、`time` 转北京时间。

> 行 `key` 仍用 `` `${r.batch}-${r.step}` ``（批次+步骤稳定，同页内唯一）。

---

## 4. 测试

### 后端 — `server/tests/test_pipeline_logs.py`

**改造现有 3 个端点测试**（响应从裸 list → envelope）：
- `test_logs_endpoint_flattens_run`、`test_logs_newest_batch_first`、`test_logs_empty_and_not_found` 改读 `.json()["items"]`；空态额外断言 `["total"] == 0`；越权仍 404。
- `build_run_log_rows` 三个纯函数单测**不动**。

**新增**：
1. **按行翻页跨批次切割**：`page_size∈{20,30}`，为测跨页切割造 ≥21 行——插 8 条 run × 每条 3 行 = 24 行。`page_size=20, page=1` → 返回前 20 行（最新批次在前；第 7 条 run 的第 2 行恰好被切到第 2 页）；`page=2` → 返回剩 4 行、`total=24`。断言 `page1.items[-1]` 与 `page2.items[0]` 在「批次-步骤」上**连续、不重、不漏**（拼接两页 == 全 24 行的前缀）。
2. **日期范围筛选（北京日边界）**：插 2 条 run，`completed_at` 分别落在北京 `2026-06-04 23:30`（UTC `15:30`）和 `2026-06-05 00:30`（UTC `2026-06-04 16:30`）。`start_date=end_date=2026-06-05` 只命中后者；`2026-06-04` 只命中前者——验证 +08:00 边界正确。
3. **页大小归一化**：`page_size=7` → 按 30 处理；`page_size=20` 生效。
4. **越界页**：`page=999` → `items=[]`、`total` 仍为真实总数。
5. **日期解析错误**：`start_date=2026-13-99` → 400。

### 前端

`pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`。可选 Playwright：登录 → 智能体管理 → 某智能体「日志」→ 断言筛选栏 / 分页器 / 滚动容器渲染、翻页后行变化。

---

## 5. 验收标准

1. 日志页顶部出现 `开始日期`/`结束日期` 两个**纯日期**输入 + `筛选`/`重置`；筛选后只剩该北京日期区间的行，回到第 1 页。
2. 表格区为**固定高度 + 内部纵向滚动**，表头吸顶；满页 30 行可在容器内滚动，筛选栏与分页器不随之滚走。
3. 分页器显示 `第 X / Y 页 · 共 N 条`，`上一页/下一页` 在边界禁用；页大小可在 20 / 30 间切换（默认 30），切换回第 1 页。
4. 翻页按**日志行**计：满 30 行换页；同一批次跨页边界时，相邻两页在「批次-步骤」上连续、不重不漏。
5. 可翻到任意早期历史（服务端分页，非仅最近 N 批）。
6. 行数据 / 着色 / 北京时间显示与 #32 一致；无任何行 → 「暂无运行日志」。
7. 不改执行引擎、不加表 / 迁移、不动审计模块；后端端点测 + 前端 typecheck/build 全绿。

---

## 6. 风险与缓解

- **响应 envelope 破坏性变更**：唯一调用方是 `AgentLogsView` + 3 个测试，同 PR 一并改，无外部消费者。
- **`JSON_LENGTH` 是 MySQL 函数**：本项目 MySQL only（CLAUDE.md 明示，无 SQLite 兼容），`func.json_length` 直接可用；测试走 `@pytest.mark.mysql` 真库。
- **节点改名 / 增删导致旧批次任务名映射不准**：#32 已知局限，沿用——缺失下标回退「步骤 N」，不崩。
- **日期 tz 依赖固定 +08:00**：产品 China-only，已与 `fmtTime` 的 `Asia/Shanghai` 一致；如未来多时区再抽配置。
- **批次被翻页切断**：用户已确认接受；保留「批次」列 + 失败批次标红，仍可辨识归属。
- **深页 `id_counts` 游走成本**：只读 `(id, 行数)` 整型且命中即 `break`；极端「无筛选 + 巨量历史 + 翻到很深」才会多走，可接受；真成瓶颈再加按 run 的二级游标。
