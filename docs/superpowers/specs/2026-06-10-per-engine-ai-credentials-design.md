# 设计：写作模型「每引擎自带密钥」——下拉一选即彻底切换 AI

- 日期：2026-06-10
- 范围：AI 生文「写作模型」的引擎选择 + 密钥联动（不含格式/配图模型）
- 状态：已评审，待写实现计划

## 背景与问题

下拉选模型的 UI **已经存在**：节点 / 方案编辑器有 `ai_engine` 类型下拉，选项来自
`GEO_AI_ENGINES` → `GET /api/generation/ai-engines`。AI生文节点（`ai_generate`）、
AI创作节点（`ai_compose`）、方案编辑器都能选。

真正坏掉的是**密钥那一侧**：无论下拉选哪个引擎，
`article_writer.py:generate_article_from_prompt()` 都把唯一的 `GEO_AI_API_KEY`
强行塞给每一次调用（`api_key=settings.ai_api_key`），并通过 `_inject_api_key()` 把这个
key `setdefault` 进 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`。结果：

- 在下拉里选「DeepSeek」，发出去的仍是 Claude 的 key → 调用失败。
- 引擎列表只带 `{label, model}`，没有 per-engine 的 key / base_url。
- 用户被迫每次手动改 `GEO_AI_MODEL` / `GEO_AI_API_KEY` 来切换厂商——即本次要消除的痛点。

**目标**：每个引擎自带自己的 `api_key`（+ 可选 `base_url`），密钥一次性在 `.env` 填好，
下拉一选就彻底切换 AI，无需再手改全局变量。

## 关键决策（评审已确认）

- **配置形态**：一个 JSON 列表 `GEO_AI_ENGINES`，每项带 `label / model / api_key / base_url`。
- **范围**：只修「写作模型」。三个生文入口（`ai_generate` / `ai_compose` / 方案运行）全部汇聚到
  同一个 `generate_article_from_prompt(model=...)`，改这一处即同时修好三处。
  格式/配图模型（`GEO_AI_FORMAT_*`）保持独立单配置，不动。
- **向后兼容**：默认引擎（`model` 为空）仍走 `GEO_AI_MODEL` + `GEO_AI_API_KEY`，
  「系统默认」行为不变。
- **下拉存值仍是 model 字符串**（方案 `ai_engine` 列 / 流水线 config 现状），已存配置零迁移、
  零 DB 改动。后端按 model 字符串查引擎。
  - 代价（已知限制）：两个引擎若 model 相同、仅 key/网关不同，区分不开。把 model 视作
    「每引擎唯一」并写入文档；不引入引擎 ID（那会动 `ai_engine` DB 列 + 所有已存配置，超范围）。
- **引擎 `api_key` 留空** → 回落到 `GEO_AI_API_KEY`（便于列多个同厂商模型不重复填 key）。

## 配置结构

`core/config.py`：

```python
class AiEngineConfig(BaseModel):
    label: str                      # 下拉展示名
    model: str = ""                 # litellm 模型串；"" = 用 GEO_AI_MODEL 默认
    api_key: str = ""               # "" = 回落到 GEO_AI_API_KEY
    base_url: str | None = None     # OpenAI 兼容网关/代理；None = litellm 默认

# Settings 里：
ai_engines: list[AiEngineConfig] = [AiEngineConfig(label="默认写作模型")]  # GEO_AI_ENGINES
```

`.env` 示例（一整个 JSON 列表）：

```bash
GEO_AI_ENGINES='[
  {"label":"Claude Sonnet","model":"claude-3-5-sonnet-20241022","api_key":"sk-ant-..."},
  {"label":"DeepSeek","model":"deepseek/deepseek-chat","api_key":"sk-deepseek-..."},
  {"label":"公司网关","model":"openai/gpt-4o","api_key":"sk-xxx","base_url":"https://oneapi.mycorp/v1"}
]'
```

pydantic-settings 把该环境变量解析成 `list[AiEngineConfig]`，**写错的项在启动时即报错**
（早炸早发现，而非生文跑一半才失败）。

## 密钥解析（纯函数，集中一处）

`core/config.py` 新增：

```python
def resolve_engine(selected: str | None) -> tuple[str, str, str | None]:
    """下拉选中的 model 串 → 实际调用参数 (model, api_key, base_url)。"""
    settings = get_settings()
    sel = (selected or "").strip()

    # 1) 空 = 系统默认引擎
    if not sel:
        return settings.ai_model, settings.ai_api_key, None

    # 2) 在 ai_engines 里按 model 串找第一个匹配
    for e in settings.ai_engines:
        if e.model == sel:
            return (
                e.model or settings.ai_model,
                e.api_key or settings.ai_api_key,   # 引擎 key 空 → 回落默认 key
                e.base_url,
            )

    # 3) 列表里没有（手填/历史值）→ 该 model + 默认 key（保持今天行为）
    return sel, settings.ai_api_key, None
```

设计为纯函数：无副作用、可单测、解析逻辑只此一份。

## 后端改动点

- `core/config.py` —— 加 `AiEngineConfig`；`ai_engines` 改 `list[AiEngineConfig]`；加 `resolve_engine()`。
- `ai_generation/article_writer.py`：
  - `generate_article_from_prompt()` 用 `resolve_engine(model)` 拿到 `(model_str, api_key, base_url)`，
    显式传给 litellm：

    ```python
    model_str, api_key, base_url = resolve_engine(model)
    response = litellm.completion(
        model=model_str,
        messages=[...],
        api_key=api_key or None,
        api_base=base_url or None,   # None → litellm 用各 provider 默认
        timeout=300,
        max_tokens=12000,
    )
    ```

  - **删除 `_inject_api_key()`**：它用 `setdefault` 把单 key 塞进 `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`，
    正是跨厂商串 key 的祸根；改为「每次调用显式传 key/base_url」后它多余且有害，移除。
- **三个生文入口不改**：`ai_generate` / `ai_compose` / `scheme_executor` 只把下拉值当 `model` 透传，
  解析全发生在 writer 内。

## 安全：绝不把 key 泄给前端

`GET /api/generation/ai-engines` 当前返回 `[AiEngineRead(**e) for e in settings.ai_engines]`。

- **`AiEngineRead` 保持只有 `label` + `model`**，永不加 `api_key` / `base_url`。
- `e` 从 dict 变成 `AiEngineConfig` 对象后，构造改为显式 `AiEngineRead(label=e.label, model=e.model)`，
  从源头杜绝把整对象铺开导致泄漏。
- 补测试钉死：响应 JSON 不含 `api_key` / `base_url`。

## 前端：零改动

下拉已用 `engines`（label/model）、存的就是 model 串（`PipelineEditor.tsx` `ai_engine` 分支、
`SchemeEditorModal`）。后端密钥解析对前端透明，前端不动。

## 错误处理

- 引擎选了但其 `api_key` 空、且 `GEO_AI_API_KEY` 也空 → 不自造错误，让 litellm 抛原生「缺 key」异常；
  单篇失败被现有逻辑收进 `errors` → 运行聚合 `partial_failed`，不影响其它篇。
- `GEO_AI_ENGINES` JSON 写错 → pydantic-settings 启动时报错。

## 测试

1. `resolve_engine()` 纯函数：空串→默认引擎；命中引擎→自带 key+url；引擎 key 空→回落默认 key；
   列表外的 model→该 model+默认 key。
2. writer 行为：monkeypatch `litellm.completion` 抓 kwargs，断言选不同引擎时 `model`/`api_key`/`api_base` 正确
   （沿用 `test_run_scheme_passes_ai_engine_model_to_llm` 套路）。
3. 配置解析：带 `api_key`/`base_url` 的 `GEO_AI_ENGINES` JSON 正确解析为 `AiEngineConfig`。
4. 防泄漏：`GET /api/generation/ai-engines` 响应不含 `api_key`/`base_url`。

## 影响面小结

- 实质改动 2 个文件：`core/config.py` + `ai_generation/article_writer.py`。
- 前端 0 改动、DB 0 迁移、向后兼容。
- 文档：`CLAUDE.md` AI 生文模块一节补一句「写作引擎含 per-engine key/base_url」（实现计划里处理）。
