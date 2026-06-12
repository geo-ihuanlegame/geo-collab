# 问题源节点内联生文规格（per-type 模板+数量）设计稿

- 日期：2026-06-10
- 分支：`feature_question_fix`
- 范围：`pipelines` 模块的 `question_source` / `ai_generate` 两个节点（前端 PipelineEditor + 后端 nodes/router）
- 关联：对齐「方案编辑（scheme）」的 per-type「问题 + 允许模板 + 文章数」模型

## 1. 背景与动机

当前流水线里「问题源 `question_source` → AI生文 `ai_generate`」的分工：

- **问题源**：选 `问题池` + `多选类型` + `精选具体问题`，把**所有选中问题（跨类型）合并成一段** `question_text` 输出。无模板、无数量。
- **AI生文**：单个 `提示词模板` + `生成数量` + `模型(自由文本)`，吃上游 `question_text`，并发生成 N 篇。

方案编辑（scheme）已有更细的模型：**每个问题类型一行 = 选问题 + 文章数 + 允许模板（多选，运行时随机抽）**。本次需求把这套 per-type 能力搬进**问题源节点**，让问题源成为「按类型配置生文规格」的源；AI生文承接执行，并在上游已配规格时**自动屏蔽**自身对应字段。顺带把 AI生文的「模型」从自由文本换成现成的 `ai_engine` 下拉。

## 2. 核心模型：问题类型 = 最小单元

「合并」只发生在**一个类型内部**（把该类型勾选的问题合并成一段 `question_text`）；类型之间永远平级、**不跨类型合并**。每个类型是一个独立的「最小生成单元（环）」，单独配置：勾哪些问题、允许哪些模板、生成几篇。

### 2.1 逐单元判定表（生成时，对问题源每张类型卡）

| 该类型卡状态 | 是否生成 | 用哪套模板 | 用哪个数量 |
|---|---|---|---|
| 没勾问题（哪怕配了模板/数量） | ❌ 弃用，不参与生文 | — | — |
| 勾了问题，无模板、无数量 | ✅ | AI生文的模板 | AI生文的数量 |
| 勾了问题 + 模板，无数量 | ✅ | 该卡模板（随机抽） | AI生文的数量 |
| 勾了问题 + 数量，无模板 | ✅ | AI生文的模板 | 该卡数量 |
| 勾了问题 + 模板 + 数量 | ✅ | 该卡模板（随机抽） | 该卡数量 |

规则要点：

- **闸门是「问题」**：只有勾了 ≥1 个问题的卡才启用；只配了模板/数量但没勾问题的卡 = 弃用，不出现在输出里。
- **模板、数量各自独立兜底**（互不绑定）：缺哪个就用 AI生文节点对应的那个补。
- **模型永远取 AI生文的 `ai_engine`**（问题源不携带模型）。
- 每张启用卡：该卡问题合并成一段 → 生成「解析出的数量」篇，每篇从「解析出的模板集」**随机抽一个**（复用 `_pick_valid_template`）。
- **总篇数 = 各启用卡解析数量之和**。

## 3. 数据契约（节点间）

问题源在保留现有 `question_text`（扁平，全部选中问题合并）/ `question_count` 之外，**新增** `generation_units`：

```jsonc
{
  "question_text": "1. q1\n2. q2 ...",   // 保留：给 ai_compose / 输入源等只认扁平文本的消费者
  "question_count": 12,                    // 保留
  "generation_units": [
    {
      "question_type": "综合通用推荐",      // 或哨兵 "__uncategorized__"
      "question_text": "1. xxx\n2. yyy",   // 该类型勾选问题预渲染成的一段（问题源侧渲染，ai_generate 不碰问题池）
      "allowed_prompt_template_ids": [3, 5],// 可空
      "article_count": 2                    // 可空 / 0 表示未配
    }
    // 只含「勾了 ≥1 个问题」的类型；弃用的类型不出现
  ]
}
```

- **为什么扁平 `question_text` 必须保留**：`ai_compose`（AI创作）节点也消费上游 `question_text`（`ai_compose.py:16`）。`generation_units` 是叠加的新字段，不破坏既有消费者。
- **预渲染放在问题源侧**：每个单元的 `question_text` 由问题源渲染好（它本就持有问题池）。AI生文保持「不直连问题池」的轻量姿态，只消费传入数据。

## 4. 后端改动

### 4.1 `question_source` 节点（`nodes/question_source.py`）

- **config 升级**为显式 per-type `units`：
  ```jsonc
  {
    "pool_id": 1,
    "units": [
      { "question_type": "综合通用推荐",
        "record_ids": ["recA", "recB"],        // 显式精选；省略/null = 整类（自动跟进新同步问题）
        "allowed_prompt_template_ids": [3, 5],  // 可空
        "article_count": 2 }                    // 可空
    ]
  }
  ```
- **向后兼容**：`units` 缺失时回落读旧字段 `question_types` / `question_record_ids` / `question_type`，并把旧选择**按问题的 `category` 分组成 per-type 单元**（每组无模板、无数量）：
  - 旧 `question_types=[A,B]`（整类）→ 单元 A、单元 B（各整类，自动跟进）。
  - 旧 `question_record_ids=[...]`（跨类型精选）→ 按各 record 的 `category` 分到对应类型单元，每单元 `record_ids` 为该类型命中的子集。
  - 两者皆空（整池）→ 按池内现有 `category` 拆成每类型一个整类单元。
  - 扁平 `question_text` 仍按所有选中问题合并产出，旧消费者无感。
- **输出**：
  - 扁平 `question_text` / `question_count`：所有启用单元问题合并（保持旧行为）。
  - `generation_units`：逐类型，仅含勾了问题的类型，每单元带预渲染 `question_text` + `allowed_prompt_template_ids` + `article_count`。
- **整类自动跟进**：单元 `record_ids` 省略/null 时取该类型当前所有 `source_active` 问题（与现有「整类」语义一致），新同步进来的问题自动纳入。

### 4.2 `ai_generate` 节点（`nodes/ai_generate_node.py`）

- **判定分支**：`ctx.inputs` 含非空 `generation_units` → 走**逐单元路径**；否则保持**现有行为**（吃扁平 `question_text` + 自身 `prompt_template_id`/`count`）。
- **逐单元路径**：对每个单元解析（与判定表一致）：
  - `templates = unit.allowed_prompt_template_ids if 非空 else [本节点 prompt_template_id]`
  - `count = unit.article_count if >0 else 本节点 count`
  - `model = 本节点 ai_engine`（即 config 里 `model` 字段，下拉存的 model 串）
  - 生成 `count` 篇，每篇 `_pick_valid_template(templates)` 随机抽一个有效模板 → `generate_article_from_prompt(question_text=unit.question_text, model=model)`。
- **总量约束**：所有单元解析数量之和 > `ai_generate_max_count` → `ValidationError`（决策 1）。
- **失败隔离**：单元解析后模板集为空（既无单元模板也无节点模板）或单篇生成异常 → 记入 `errors`、跳过该篇/该单元，其它继续；整体聚合为 `partial_failed`（决策 2）。
- `model` 仍从 `cfg.get("model")` 读，无需改读取逻辑（前端把下拉值写进 `config.model`）。

### 4.3 node-types schema（`router.py:get_node_types`）

- `question_source`：per-card 的「允许模板（多选）」+「文章数」由前端 `QuestionTypePicker` 统一渲染——**沿用现有做法、扩展既有 `question_types` 字段分支**（该字段已被 picker 接管、`question_records` 已 `return null` 不单独渲染），picker 额外写 `units`，**不新增字段 type**，schema 无需为此增条目。
- `ai_generate`：`{"key": "model", "type": "text"}` → `{"key": "model", "type": "ai_engine", "label": "模型"}`。

## 5. 前端改动（`web/src/features/pipelines/PipelineEditor.tsx`）

### 5.1 `QuestionTypePicker`

- 现状：把勾选压成「最小扁平 config」（`question_types` 或全局 `question_record_ids`），**丢失 per-type 分组**——无法承载 per-type 模板/数量。
- 改造：config 改为显式 per-type `units`。每张类型卡在「选择问题」下新增：
  - **允许模板**：多选下拉（复用 `genTemplates`，与 `prompt_templates` 字段同源）。
  - **文章数**：数字输入。
- 受控写回 `units`；保留「全选某类型 = 整类（record_ids 省略，自动跟进）」与「部分勾选 = 显式 record_ids」的区分（嵌进每个 unit）。
- 读旧 config（`question_types`/`question_record_ids`）做一次性兼容映射到 `units` 视图。

### 5.2 `ai_generate` 面板

- **模型**：`ai_engine` 下拉（`engines` 已加载，复用 `ai_compose` 同款渲染）。
- **模板 / 数量 字段独立灰显**：
  - 通过 `flow_meta.dependsOnIndex` 找上游节点；若上游是 `question_source` 且有 `units`：
    - `templateCovered = 每个启用单元（有问题）都自带非空 allowed_prompt_template_ids`
    - `countCovered = 每个启用单元都有 article_count > 0`
    - `templateCovered` → 灰禁「模板」字段；`countCovered` → 灰禁「数量」字段；二者独立判定。
    - 顶部标注「已由上游问题源接管」。
  - 上游非 question_source / 无 units → 不灰显，正常可编辑。

## 6. 决策（开放点定档，供 review）

1. **总量超限**：解析总篇数 > `ai_generate_max_count` → **报错 `ValidationError`**（与现有 `ai_generate` 一致，不静默截断，避免少生成用户预期的篇数）。
2. **单元缺模板（兜底后仍空）**：**记错误、跳过该单元**，其它单元继续，聚合 `partial_failed`（与 `ai_generate`/`ai_compose` 的「单篇/单元失败隔离」一致）。
3. **单元问题表示**：`record_ids` 省略/null = 整类（自动跟进新同步问题）；显式列表 = 精选。保留现有「整池/整类自动跟进」能力，不退化为死名单。
4. **扁平 `question_text` 保留**：兼容 `ai_compose` 等消费者，`generation_units` 为叠加字段。

## 7. 兼容与边界

- 旧流水线「问题源选多类型 → AI生文」：按确认的「逐类型」语义生效，多类型时产量 = 各类型篇数之和（可能比现在多，已确认可接受）。
- AI生文跟在「输入源」后（无 `generation_units`）→ 行为完全不变。
- 只配模板/数量没勾问题的类型 → 不进 `generation_units`（弃用）。
- `ai_compose` 本次不改逻辑（仅继续复用 `_pick_valid_template`）。
- 运行快照：流水线 run 冻结节点快照（`PipelineRun.snapshot`），本设计不改快照机制；问题源在创建 run 时已固化 config，执行只读快照——`generation_units` 在节点执行期由问题源即时产出、随 `node_results` 落库。

## 8. 测试计划（MySQL only，沿用 `build_test_app`）

后端（`server/tests/`）：

- 问题源：`units` 输出 `generation_units`（含预渲染 `question_text`、模板、数量）；旧 `question_types`/`question_record_ids` config 兼容；弃用类型（无问题）不出现；整类自动跟进。
- AI生文逐单元：判定表五行各一例（弃用 / 全兜底 / 仅模板 / 仅数量 / 全自带）；总量上限报错；单元缺模板跳过 + partial_failed；模型取 `ai_engine`。
- 复用现有 `test_question_source_multiselect.py` / `test_pipeline_node_types.py` 的夹具风格，新增 `test_question_source_units.py`（或并入既有文件）。

前端：无单测框架，靠 `pnpm --filter @geo/web typecheck` + `build` 把关；手动验证 picker per-card 模板/数量、ai_generate 灰显与模型下拉。

## 9. 不在本次范围

- `ai_compose` 节点的功能调整。
- 问题池 / 方案（scheme）本身的改动。
- 联网兜底、配图等其它节点。
- 模型清单的运维配置（`GEO_AI_ENGINES` 由用户在 `.env` 维护，非代码改动）。
