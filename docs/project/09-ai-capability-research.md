# 09 · AI 能力与工程实践

> 关联文档：[01 需求分析](./01-requirements-analysis.md) · [03 技术架构](./03-technical-architecture.md) · 仓库 `AI_GENERATION.md`
>
> 本文记录项目中 AI 能力的落地与工程选型，结论均以代码为准。

---

## 1. 两个层面的 AI 应用

| 层面 | 含义 | 本项目证据 |
|------|------|-----------|
| A. 产品内 AI | 把 AI 作为功能交付给运营 | AI 生文模块（LiteLLM + LangGraph）、AI 智能排版（小标题识别 + 自动配图） |
| B. 交付过程 AI | 用 AI 辅助设计与实现 | `docs/superpowers/` 下的 spec → plan 协作产物 |

---

## 2. 产品内的 AI 能力

### 2.1 AI 生文管线（LiteLLM + LangGraph）

LangGraph 编排的三段流程（`ai_generation/pipeline.py`）：

```
planner_node      准备任务清单（task_specs 由 _build_task_specs 构建）
      ▼
parallel_write_node   ThreadPoolExecutor(max_workers=4) 并发写作
      · 每篇调 litellm.completion(GEO_AI_MODEL) 生成 Markdown
      · 提取 # 标题 → markdown_to_tiptap / markdown_to_html（converter.py）
      · create_article() 落 articles 表；问题库 item 标记 consumed
      ▼
finalize_node     会话 status = done / failed
```

- **选题闭环**：问题库从飞书多维表同步（`feishu_app_token` + `feishu_table_id`），手动选题按分类分组、自动模式按 `CategoryUsage`"最久未用分类优先"轮取，打通"选题 → 成稿 → 入库"。
- **幂等**：`client_request_id` 做并发重试幂等；批次元数据存独立 `generation_sessions`（`article_ids` JSON）。

### 2.2 AI 智能排版

`articles/ai_format.py` 用格式模型识别正文小标题（段落升级为 h2，从不降级既有标题），并可按图库分类自动插图；状态经 `ai_checking` / `ai_format_error` 暴露，后台线程执行、前端轮询。直接自动化"平台要求的小标题 + 配图"这类重复排版（痛点 P4）。

---

## 3. AI 工程选型结论

| 决策 | 选择 | 理由 |
|------|------|------|
| 模型网关 | **统一走 LiteLLM**，禁止直连 `anthropic` / `openai` SDK | 换模型零代码改动、便于成本切分与灰度 |
| 模型分档 | **双模型**：主写作 `GEO_AI_MODEL`（默认 claude-3-5-sonnet）、格式/标题/配图 `GEO_AI_FORMAT_MODEL`（默认 deepseek-v4-flash） | 把"高价值创作"与"低价值结构化处理"用不同档位模型分离，质量与成本兼顾 |
| 并发 / 一致性 | plan 阶段**顺序执行**且独占共享文件；写作 agent **并发**（max_workers=4）且不碰共享文件 | 兼顾吞吐与产物一致性 |
| 执行位置 | 生文跑在 **API server 后台线程**（无独立 worker） | 复用 DB session 工厂，免额外进程；`client_request_id` 幂等支持重试 |
| 超时 | 格式模型独立超时 `GEO_AI_FORMAT_TIMEOUT_SECONDS`（默认 120） | 与主写作解耦，避免互相拖累 |

> 设计 rationale 与 LangGraph 图详见仓库 `AI_GENERATION.md`；实现见 [03 §7 AI 管线](./03-technical-architecture.md)。

---

## 4. 交付过程中的 AI 协作

仓库 `docs/superpowers/` 留存了"**先设计、后实现**"的协作产物：

- `docs/superpowers/specs/` —— 设计规格（如 image-library-ui-upgrade-design、user-testing-fixes-design）。
- `docs/superpowers/plans/` —— 对应的实现计划（ai-format-adjustment、toutiao-rich-format、frontend-multi-stock-category 等）。

工作流形态：**需求 → spec（设计规格）→ plan（实现计划）→ 实现 → 审查**。这套"分段确认"的协作方式降低了返工，并把设计决策沉淀为可追溯的文档。

---

## 5. 小结

- **产品内 AI** 已作为核心能力落地（生文 + 智能排版），并有清晰的工程选型：LiteLLM 统一网关、双模型成本分档、并发/一致性边界。
- **交付过程 AI** 形成了 spec → plan → 实现 → 审查的可复用协作流，产物留痕在 `docs/superpowers/` 与本文档集。

---

> 下一篇：[10 交接 Runbook](./10-handover-runbook.md)。
