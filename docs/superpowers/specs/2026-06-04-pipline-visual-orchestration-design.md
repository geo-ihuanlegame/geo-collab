> ⚠️ **已作废 (SUPERSEDED 2026-06-04)**：本方案针对 `content-library-public` / `pc-admin-conetnt-library-public` 两个**参考项目**编写。后经确认这两个项目仅供架构参照、不可改动，实际改造落在 `geo-collab` 主仓库。请改看 `2026-06-04-geo-collab-pipeline-orchestration-design.md`。以下内容仅作架构参考保留。

---

# 可视化流程编排轻量增强 — 设计方案（已作废）

- **日期**：2026-06-04
- **聚焦模块**：可视化流程编排（pipLine / 智能体广场 / 工作流）
- **涉及工程**：
  - 前端：`pc-admin-conetnt-library-public`（Vue + view-ui-plus + vant）
  - 后端：`content-library-public`（Java / Spring Boot + MyBatis，MySQL）
- **技术路线决策**：不引入图库，在现有线性 pipLine 之上做轻量增强（向后兼容）

> 本 spec 只覆盖"可视化流程编排"一个模块。原始需求中的其余五块（智能体管理、内容审核库链路、AI 生文节点、内容分发节点、端到端链路）各自另起 spec，不在本次范围内。

---

## 1. 背景与现状

现有 pipLine 是一个**线性顺序流水线**，不是自由画布 DAG：

- 节点（`pip_line_item`）按 `item_index` 排序，`item[0]` 为 source，`item[1..n]` 为 work 节点。
- 节点连接是**隐式**的 `item[i] → item[i+1]`，前端用静态 SVG `FlowLine` 画相邻连线，**无边表、无连线配置**。
- 后端执行器 `PipLineRunServiceImpl` 严格线性遍历：source 产出 `List<TextPipLineContext>`，再依次过 work 节点；节点类型经 Spring `serviceMap.get(code)` 分发。
- 节点 `data` 字段是插件表单的 JSON map（`Map<String,String>`），各节点独立读取自己的配置，**节点间无自动数据传递**。
- 无图库（仅 `vuedraggable` 用于列表排序）。

### 现状能力对照（Section 三 需求）

| 需求 | 现状 |
|---|---|
| 节点属性面板（选中编辑参数） | ✅ 已有（抽屉 + 插件动态表单） |
| 工作流唯一标识符 | ✅ 已有（`pip_line.id`） |
| 保存工作流 | ✅ 已有 |
| 拖拽节点自由编排 | ❌ 仅线性泳道，无画布 |
| 连线配置依赖关系 + 数据传递规则 | ❌ 连线隐式/静态，无数据传递配置 |
| 草稿暂存 | ❌ 缺失（每次改动即时落库） |
| 版本回溯 | ❌ 缺失 |

### 关键事实（实现时依据）

- `pip_line` 表列：`id, account_id, common, name, type, cron, cron_name, ignore_exception, run_status, run_result, start_run_time, end_run_time, description, create_time, update_time`（DDL 见 `content-library-public/sql/工作流dml.sql:35`）。
- `pip_line_item` 表列：`id, account_id, pip_line_id, icon, name, code, item_index, create_time, update_time`。**注意：实体 `PipLineItem.java` 含 `data` 字段，但 DDL 段未显式列出 `data` 列**——实现第一步必须先核对线上表是否已有 `data` 列（很可能后续迁移补过），以此决定 `data` 是否需要随迁移补列。
- 实体继承 `BaseAutoIncrementEntity`（提供 `id` / `create_time` / `update_time`）。
- 节点新增 DTO `PipLineItemAddRequest` 接收 `Map<String,String> data`；更新 DTO `PipLineItemUpdateRequest` 仅 `id + data`。
- pipLine REST 前缀均为 POST：`pipLine/*`、`pipLineItem/*`、`pipLinePlugin/*`、`pipLineLog/*`。

---

## 2. 设计目标与非目标

### 目标（本次交付）
1. **连线依赖 + 数据传递规则**：节点可配置"从上游某节点的输出字段映射到本节点输入字段"，并可配置跳过条件；执行器在线性遍历中按规则注入输入、按条件跳过。
2. **草稿暂存**：编辑态可"保存草稿"而不影响线上运行版本；"发布"才生效。
3. **版本回溯**：每次发布生成快照；可查看历史版本、回溯（载入草稿后由用户确认再发布，不直接覆盖线上）。
4. **属性面板完善**：在现有抽屉内增加"数据传递"分栏、必填校验提示、上游来源展示，风格与 view-ui-plus 一致。

### 非目标（明确排除，YAGNI）
- 不引入 LogicFlow / AntV X6 / Vue Flow，不做自由画布 DAG。
- 不做分支 / 循环 / 多上游合流（数据传递仅支持线性单上游）。
- 不改执行模型的线性本质。
- 不触碰其余五大模块。

---

## 3. 详细设计

### 3.1 连线元数据（数据传递与依赖）

**存储**：`pip_line_item` 新增列 `flow_meta`（JSON / longtext，nullable），与插件表单 `data` 分开存，避免污染表单字段。结构：

```json
{
  "dependsOnIndex": 1,
  "inputMapping": [
    { "from": "title", "to": "sourceTitle" }
  ],
  "condition": { "field": "status", "op": "eq", "value": "ok" }
}
```

- `dependsOnIndex`：上游节点的 `item_index`（默认上一个节点；可指定更早的单个节点）。
- `inputMapping`：上游输出字段 → 本节点输入字段的映射数组。
- `condition`：可选，条件不满足则跳过本节点。`op` 初版支持 `eq` / `neq` / `contains`（够用即可，避免过度设计）。

**前端**：节点属性抽屉新增"数据传递"分栏：
- 上游节点下拉（来自 `dependsOnIndex`，候选为本节点之前的节点列表）。
- 字段映射表格：`from` 下拉来源为上游节点插件 `form` 定义的输出字段，`to` 下拉来源为本节点插件 `form` 定义的输入字段。
- 条件配置（可选，可折叠，默认不展开）。

**后端执行**：`PipLineRunServiceImpl` 在跑每个 work 节点前：
1. 按 `dependsOnIndex` 取上游累积上下文（现有 `TextPipLineContext`）。
2. 按 `inputMapping` 把上游输出字段注入本节点输入。
3. 若 `condition` 不满足则跳过该节点并记 INFO 级 `pip_line_log`。

这是对现有线性遍历的一处受控改动，不改变线性本质。无 `flow_meta` 的旧节点行为完全不变（向后兼容）。

### 3.2 草稿暂存（draft）

**存储**：`pip_line` 新增两列：
- `draft_snapshot`（longtext，nullable）：编辑态画布的全量 JSON（items + 各自 flow_meta）。
- `has_draft`（smallint，默认 0）：是否存在未发布草稿。

**行为**：
- **保存草稿**：把当前编辑态序列化进 `draft_snapshot`，置 `has_draft=1`，**不动** `pip_line_item`；运行仍用已发布版本。
- **发布**：把 `draft_snapshot` 应用到 `pip_line_item`（重建该 pipLine 的 item 行 = 删除旧行 + 按草稿插入新行，单事务），清空 `draft_snapshot` 置 `has_draft=0`，并写一条版本快照（见 3.3）。
- **丢弃草稿**：清空 `draft_snapshot` 置 `has_draft=0`。

**新增端点**：
- `POST pipLine/saveDraft` — body `{ pipLineId, snapshot }`
- `POST pipLine/publishDraft` — body `{ pipLineId, remark? }`
- `POST pipLine/discardDraft` — body `{ pipLineId }`

> 兼容：现有"即时增删改节点"端点（`pipLineItem/*`）保留。草稿流是叠加的可选编辑模式，前端在"编辑工作流"场景优先走草稿流；运行/调度路径读 `pip_line_item` 不变。

### 3.3 版本回溯（version）

**新增表 `pip_line_version`**：

```
id            bigint PK auto_increment
pip_line_id   bigint        not null   -- 所属工作流
version_no    int           not null   -- pipLine 内递增版本号（1,2,3…）
snapshot      longtext      not null   -- 该版本全量 items + flow_meta 的 JSON
remark        varchar(255)  null       -- 发布备注
created_by    bigint        not null   -- account_id
create_time   datetime      default CURRENT_TIMESTAMP
-- index: (pip_line_id, version_no)
```

- 每次**发布**写一条快照，`version_no` 在该 pipLine 内自增。
- **回溯安全策略**：回溯不直接覆盖线上，而是把指定版本快照载入 `draft_snapshot`（置 `has_draft=1`），由用户在编辑器确认后再"发布"——发布会生成新版本号，形成线性版本历史。

**新增端点**：
- `POST pipLineVersion/list` — body `{ pipLineId }` → 版本列表（version_no / remark / create_time / created_by）
- `POST pipLineVersion/detail` — body `{ id }` → 单版本快照
- `POST pipLineVersion/rollback` — body `{ id }` → 把该版本快照载入草稿

**唯一标识符**：`pip_line.id` 已满足 spec 的"唯一标识符"要求，不额外引入 guid。

### 3.4 属性面板完善

现有抽屉 + 动态表单保留，增强：
- 新增"数据传递"分栏（见 3.1）。
- 必填字段校验提示。
- 抽屉顶部显示节点 `code` / `item_index` 与上游来源摘要。
- 保持 view-ui-plus 组件风格，不重写抽屉。

---

## 4. 改动清单（双工程）

### 后端 `content-library-public`
- **DDL 增量迁移**（写进 `sql/` 新文件 + 追加 `migration.sql`）：
  - `pip_line_item` 加 `flow_meta` 列（先核对 `data` 列是否已存在，缺则一并补）。
  - `pip_line` 加 `draft_snapshot` / `has_draft` 列。
  - 新建 `pip_line_version` 表。
- **实体**：`PipLine` 加 `draftSnapshot` / `hasDraft`；`PipLineItem` 加 `flowMeta`；新增 `PipLineVersion` 实体 + MyBatis mapper。
- **DTO/请求**：草稿三端点、版本三端点的 request/response。
- **Controller**：`PipLineController` 加 `saveDraft` / `publishDraft` / `discardDraft`；新增 `PipLineVersionController`。
- **Service**：草稿快照应用（重建 item 行，单事务）、版本快照写入/回溯；`PipLineRunServiceImpl` 接入 `inputMapping` / `condition`。

### 前端 `pc-admin-conetnt-library-public`
- `src/views/pipLine/list/item/PipLineItem.vue`：属性抽屉加"数据传递"分栏；新增"保存草稿 / 发布 / 丢弃草稿"按钮与状态标识（`has_draft`）。
- 新增"版本历史"抽屉组件（列表 + 查看 + 回溯）。
- `src/api/index.js`：加 6 个新端点的 API 封装。

---

## 5. 关键决策（已与用户确认）

1. **回溯不直接覆盖线上**：载入草稿后由用户确认再发布，更安全。
2. **数据传递仅支持线性单上游**：与线性执行模型一致，不支持多上游合并。

## 6. 风险与缓解

- **`data` 列是否已存在于线上表**：实现首步必须核对，避免迁移冲突。
- **草稿流与即时编辑端点共存**：明确"编辑器走草稿流、运行读 live item 行"的边界，避免两套写路径互相覆盖。文档化并在 PR 描述中说明。
- **快照 JSON 结构演进**：`snapshot` 内嵌版本标记字段（如 `schemaVersion`），便于未来结构升级时兼容旧快照。
- **向后兼容**：所有新列 nullable / 有默认值；无 `flow_meta` / 无草稿的旧工作流行为不变。

## 7. 验收标准（本模块）

1. 节点属性抽屉可配置数据传递（上游字段 → 本节点字段），保存后 `flow_meta` 正确落库。
2. 执行时按 `inputMapping` 注入输入、按 `condition` 跳过节点，并在 `pip_line_log` 留痕。
3. 保存草稿不影响线上运行版本；发布后线上 `pip_line_item` 与草稿一致并生成新版本号。
4. 版本列表可见历史发布；回溯将选定版本载入草稿，用户发布后形成新版本。
5. 旧工作流（无 flow_meta / 无草稿）运行行为与改动前一致。
