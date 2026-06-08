# 「已审核分组源」自动选组优化 — 设计方案

- **日期**：2026-06-08
- **聚焦**：把 `article_group_source`（界面名「已审核分组源」）的"内容分组"从**必须手动选**优化为**默认自动选最早一个未分发分组**，并只发该组「已审核 + 未分发」的文章，减少人工。
- **落地工程**：`geo-collab` 主仓库。
- **前置依赖**：**PR #34（自动分发内容：`article_round_robin` 任务类型 + `distribute` 消费 `article_ids` 路径）**。本特性的推荐接线走 `article_round_robin`，依赖 #34。实现分支 `feat/group-source-auto` 基于 `feat/auto-distribute`（#34 分支）；#34 合并后本分支 rebase 到最新 main 再开 PR。

> **愿景对齐**：人工只在内容管理审核，下游分发由工作流定时自动触发。本优化让"已审核分组源 → 分发"无需每次手动挑分组——定时跑时自动按 FIFO 取最早一批未分发的已审内容发出去。

---

## 1. 现状与问题

`article_group_source` 现状（`server/app/modules/pipelines/nodes/article_group_source.py`）：
- 必须配置 `group_id`（或上游注入），否则 `raise ValidationError`。
- 输出 `{group_id, article_ids}`，其中 `article_ids` = 该组**所有未删除**文章（不分审核/分发状态）。
- 现有 review/distribute 流程把 `group_id → group_id` 映射给 distribute，走 `group_round_robin`（整组）。

问题：每次都要手动挑一个分组，不适合无人值守的定时自动分发。

---

## 2. 目标 / 非目标

### 目标
1. `group_id` 配置改为**可选**：留空 → 自动按 FIFO 选组；填了 → 手动指定该组。
2. **自动选组（FIFO）**：选「创建最早、且含 ≥1 篇『已审核(approved) + 未分发(无 PublishRecord)』文章」的分组（未删、owner/admin）。
3. 两种模式都输出 `article_ids` = **所选组里 approved + 未删 + 未分发** 的文章（按 `sort_order`），并保留 `group_id`（所选组，供追溯/日志）。
4. 无符合条件的组 → 输出空 `article_ids` → distribute 安静跳过（定时跑无新内容不报错）。
5. 推荐接线 `inputMapping: article_ids → article_ids` → distribute 走 `article_round_robin`（只发已审子集）。

### 非目标（YAGNI）
- 不新增"分组已分发"标记列/表（用 `PublishRecord` 判定，与 `approved_content_source` 一致）。
- 不加条数上限（分组本身即一个批次，规模有界）。
- FIFO 固定按 `ArticleGroup.created_at`（次序 tiebreak `id`），不做可配置排序。
- 不改 `distribute` 节点（#34 已支持 `article_ids` 路径）。

---

## 3. 关键定义（已与用户确认）

- **「未分发」** = 该文章不存在任何 `PublishRecord`（`Article.id NOT IN (SELECT article_id FROM publish_records)`）。`PublishRecord.article_id` 为 NOT NULL，`NOT IN` 安全。
- **「已审核」** = `Article.review_status == "approved"`。
- **候选文章** = `review_status == "approved"` AND `is_deleted == False` AND (owner `user_id == ctx.user_id` 或 admin) AND 未分发。
- **符合条件的分组** = 未删 + owner/admin + 含 ≥1 篇候选文章。

---

## 4. 节点新行为（`article_group_source.py`）

config：
- `group_id: int | None`（可选）。留空 → 自动；填了 → 手动。

行为（自建 session）：
1. **确定 `chosen_group_id`**：
   - 手动（`group_id` 已配置/上游注入）：用该值。校验分组存在、未删、owner/admin，否则 `ValidationError`（与现状一致）。
   - 自动（`group_id` 为空）：在「符合条件的分组」中按 `ArticleGroup.created_at ASC, id ASC` 取第一个。若无 → `chosen_group_id = None`。
2. **计算 `article_ids`**：`chosen_group_id` 那个组里的候选文章（approved + 未删 + 未分发），`JOIN ArticleGroupItem` 按 `sort_order ASC`。若 `chosen_group_id is None` → `[]`。
3. 输出 `NodeResult(output={"group_id": chosen_group_id, "article_ids": [...]}, article_ids=[])`。空集合是正常结果（无新内容）。

> **向后兼容**：手动模式下 `group_id` 输出＝配置值不变，现有「`group_id → group_id` → `group_round_robin`(整组)」的流程**仍可用、行为不变**（distribute 优先 `article_ids`，仅当 inputMapping 映射 `article_ids` 时才走子集）。本特性只是：① `group_id` 不再必填（留空＝自动）；② `article_ids` 输出从"全部未删"收窄为"approved+未分发子集"——只有映射 `article_ids` 的流程会感知到这一变化（更正确）。

---

## 5. node-types / 前端

- `get_node_types()` 里 `article_group_source` 的 `group_id` 字段 label 改为：`"内容分组（留空＝自动选最早未分发分组）"`，类型仍 `article_group`。
- **前端无代码改动**：`article_group` 字段渲染本就含「选择分组」空选项；选空 → `onChange` 写 `undefined` → config 无 `group_id` → 节点自动模式。label 来自后端 node-types。

---

## 6. 测试（`server/tests/`，`@pytest.mark.mysql`）

新增 `server/tests/test_group_source_auto.py`（或并入既有分组测试文件，实现时择优）：
1. **自动 FIFO 选组**：建两个分组 G1（早）、G2（晚），各含已审核文章；run 节点（config 无 group_id）→ 输出 `group_id == G1`，`article_ids` = G1 的候选文章。
2. **只取 approved + 未分发子集**：某组含 approved-未发、approved-已发(预置 PublishRecord)、pending 三类 → 自动/手动选该组 → `article_ids` 只含 approved-未发那篇。
3. **FIFO 跳过无候选的更早组**：G1 全部已分发（有 PublishRecord）、G2 有候选 → 自动选 G2（G1 无候选被跳过）。
4. **无符合组 → 空**：所有组的 approved 文章都已分发 → 自动模式输出 `article_ids == []`、`group_id is None`，不报错。
5. **手动模式仍校验**：配置了不存在/非 owner 的 `group_id` → `ValidationError`。
6. **回归**：现有 `test_pipeline_review_distribute.py`（`group_id → group_round_robin` 整组路径）保持绿。

---

## 7. 验收标准
1. 节点 `group_id` 留空时自动选「最早一个含 已审核+未分发 文章」的分组；填了则用该组；都只输出该组 approved+未分发 子集为 `article_ids`。
2. `article_ids → article_round_robin` 接线下，定时跑自动按 FIFO 逐批发已审内容；发过的不再发。
3. 无新内容 → 空 article_ids → distribute 跳过、run 不失败。
4. 现有 `group_id → group_round_robin` 整组流程不受影响。
5. 不新建表/列、不改审核模型；后端测 + 现有回归全绿；前端 typecheck/build（label 变更无代码改动，仅确认 build 通过）。

## 8. 风险与缓解
- **后台线程 session**：节点自建 session、本线程 close（与现有节点一致）。
- **去重子查询**：`PublishRecord.article_id` 有索引且 NOT NULL，`NOT IN` 安全。
- **部分分发的组**：组里 approved 文章分两次审/发时，同组会被多次 run 逐步发完（每次只发当时 approved+未发的），符合预期。
- **#34 依赖**：实现基于 `feat/auto-distribute`；#34 合并后 rebase 到 main 再开 PR，避免 squash 后历史发散。
