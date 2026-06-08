# 「问题源」节点 多选类型 + 选具体问题 — 设计方案

- **日期**：2026-06-08
- **聚焦**：把 `question_source`（界面名「问题源」）的问题选择从"单个问题类型"升级为**多选问题类型 + 可选精选具体问题**，交互保持简洁（方案A：两个联动紧凑多选）。
- **落地工程**：`geo-collab` 主仓库。
- **前置**：PR #30（`question_source` 支持「全部类型/未分类」）已在 main；`/question-pools/{id}/question-types` 接口已返回"分类 + 各分类问题"。

> **愿景对齐**：定时自动跑时，运营希望灵活控制"这个生文智能体从哪些问题类型、甚至哪些具体问题取题"。多选类型满足"按板块批量"，精选具体问题满足"只发某几条"。

---

## 1. 现状

`question_source` 现 config 仅 `{pool_id, question_type}`：`""`=全部 / `"__uncategorized__"`=未分类 / 具体某类（单选）。节点按 category 过滤 `source_active` 问题，拼成编号 `question_text` 列表输出。

数据层已足够（零新增）：`/api/generation/question-pools/{id}/question-types` 返回 `QuestionType[]` = `{question_type, count, questions:[{id, record_id, question_text}]}`。问题以 `record_id`（飞书稳定 id，跨同步不变）标识，与方案编辑器一致。

---

## 2. 目标 / 非目标

### 目标
1. `question_types: string[]` 多选问题类型（含 `__uncategorized__` 表示未分类）；空/缺省 = 全部类型。
2. `question_record_ids: string[]` 可选精选具体问题（按 record_id）；空/缺省 = 上述类型下全部。
3. 交互方案A：两个联动紧凑多选框（类型多选 + 具体问题多选，后者范围随前者联动）。
4. 向后兼容老配置的单个 `question_type`（无 DB 迁移）。

### 非目标（YAGNI）
- 不复刻方案编辑器的分组卡片（方案B）。
- 不做"按类型分别精选/排除"的二维结构；精选是跨所选类型的一维 record_id 列表（选了即只发这些）。
- 不改其它节点；不改 `/question-types` 接口。

---

## 3. 节点取数语义（`question_source.py`，优先级清晰）

config：`{pool_id, question_types?: string[], question_record_ids?: string[]}`（另兼容旧 `question_type?: string`）。

1. `pool_id` 必填，校验池存在 + owner/admin（同现状）。
2. 基础查询：该池 `source_active == True` 的 `QuestionItem`。
3. **若 `question_record_ids` 非空** → 精确取 `record_id IN question_record_ids`（snapshot 语义；忽略类型）。失效（飞书已移除→`source_active=False` 或不存在）的 record_id **宽容跳过、不报错**（自动跑不因单条失效而失败）。
4. **否则**按 `question_types` 过滤（live）：
   - 解析多选类型：普通分类 → `category IN [...]`；含 `__uncategorized__` → 额外 `OR category IS NULL`；二者可并存（`category IN (...) OR category IS NULL`）。
   - `question_types` 空/缺省 → 不按类型过滤（全部）。
5. **向后兼容**：若 `question_types` 缺省但旧 `question_type` 存在 → 映射：`""`→全部（[]）、`"__uncategorized__"`→`["__uncategorized__"]`、其它→`[该类型]`。
6. 输出不变：`order_by(id asc)` 后拼 `question_text` 编号列表 → `{question_text, question_count}`。空集合是正常结果（下游 ai_compose 自行跳过）。

---

## 4. node-types / 前端（方案A）

### node-types（`get_node_types()` 改 question_source 的 config_schema）
```python
{"type": "question_source", "label": "问题源", "config_schema": [
    {"key": "pool_id", "type": "question_pool", "label": "问题池"},
    {"key": "question_types", "type": "question_types", "label": "问题类型（多选，留空=全部）"},
    {"key": "question_record_ids", "type": "question_records", "label": "具体问题（可选，留空=上述类型全部）"},
]}
```
（移除旧的单选 `question_type` 字段项。）

### 前端（`PipelineEditor.tsx`）
- **数据缓存**：现 `typesByPool` 存 `{value,label}[]`（仅分类，PR#30）。扩展为缓存该池完整 `QuestionType[]`（含每类的 questions），`ensureTypes` 改为存原始结构；从中既能取分类列表，也能按类型取问题列表。
- **新增两个字段渲染类型**：
  - `question_types`：多选（复用 `peMultiSelect` 模式），选项＝该池分类 + 「未分类」(value `__uncategorized__`)。值 `string[]`。
  - `question_records`：多选，选项＝**已选类型**下的问题（无所选类型时列全部类型的问题），option value=`record_id`、label=`question_text`。值 `string[]`。范围随 `sel.config["question_types"]` 联动。
- 旧单选 `question_type` 渲染分支**保留**（不再被 question_source 使用），避免改动牵连。
- 选空类型 → `question_types` 写 `[]`/不写；选空问题 → `question_record_ids` `[]`/不写（节点按"全部"处理）。

> 前端不算 record_id 的任何哈希、不做校验；失效条目的容错在节点运行时处理。

---

## 5. 向后兼容与迁移
- **无 DB 迁移**（config 是 JSON）。
- 已发布的旧 question_source 节点：config 里是 `question_type`（单值）。节点运行时按 §3.5 映射，行为不变。编辑器打开旧节点：新字段 `question_types`/`question_record_ids` 为空（不显示旧值），但节点仍按旧 `question_type` 跑；用户重新编辑保存后写入新键（旧 `question_type` 残留在 config 中无害，节点优先新键）。

---

## 6. 测试

### 后端（`@pytest.mark.mysql`）新增 `server/tests/test_question_source_multiselect.py`：
1. **多选类型**：池含 美食/旅游/科技 三类，`question_types=["美食","旅游"]` → 只取这两类，按 id 序。
2. **含未分类**：`question_types=["美食","__uncategorized__"]` → 美食 + category 为 NULL 的。
3. **精选覆盖类型**：`question_types=["美食"]` + `question_record_ids=[rec_of_旅游A]` → 只取该旅游问题（record_ids 优先、忽略类型）。
4. **失效 record_id 宽容**：record_ids 含一个 `source_active=False`/不存在的 → 跳过、取其余、不报错。
5. **向后兼容**：仅传旧 `question_type="美食"` → 等价 `question_types=["美食"]`。
6. **空 = 全部**：`question_types` 与 `record_ids` 均空 → 取整池 active。
（保留 PR#30 既有 `test_ai_generation_nodes.py::test_question_source_*` 全绿。）

### 前端
`pnpm --filter @geo/web typecheck && build`。可选 Playwright：节点面板出现"问题类型(多选)"+"具体问题"两个多选，后者随类型联动。

---

## 7. 验收标准
1. 节点支持多选问题类型（含未分类）；留空=全部。
2. 可在所选类型范围内精选具体问题（record_id）；选了＝只发这些（snapshot），未选＝该类型全部（live，自动纳入飞书新增）。
3. 失效 record_id 不致运行失败。
4. 旧单选 `question_type` 配置行为不变。
5. 交互＝两个联动紧凑多选；不改其它节点/接口；后端测 + PR#30 回归 + 前端 build 全绿。

## 8. 风险与缓解
- **record_ids 与类型并存的语义**：明确"record_ids 非空即覆盖类型、只发这些"，文案与 label 标注"留空=上述类型全部"。
- **typesByPool 结构变更牵连 question_type 单选渲染**：保留旧渲染分支并适配新缓存结构（从 `QuestionType[]` 取分类），确保未改到的地方不回归。
- **后台线程 session**：节点自建 session、本线程 close（同现状）。
- **大池问题多**：`question_records` 多选列表可能较长；v1 直接平铺（与方案编辑器一致），如需搜索后续再加（YAGNI）。
