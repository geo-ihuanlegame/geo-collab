# 写作模型「每引擎自带密钥」Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 AI 生文下拉选中的写作引擎自带自己的 `api_key`（+ 可选 `base_url`），下拉一选即彻底切换 AI，无需再手改全局 `GEO_AI_MODEL`/`GEO_AI_API_KEY`。

**Architecture:** 把 `GEO_AI_ENGINES` 列表项从 `{label, model}` 扩成带 `api_key`/`base_url` 的类型化 `AiEngineConfig`；新增纯函数 `resolve_engine(model串) → (model, api_key, base_url)`；让唯一的写作内核 `generate_article_from_prompt` 调它并把凭据显式传给 litellm。三个生文入口（AI生文 / AI创作 / 方案运行）都汇聚到这个内核，故零改动即同时生效。前端下拉、DB、迁移均不动。

**Tech Stack:** FastAPI + pydantic-settings v2、LiteLLM、pytest（MySQL only 的集成测试 + 无 DB 的纯函数测试）。

---

## 设计依据

详见 spec：`docs/superpowers/specs/2026-06-10-per-engine-ai-credentials-design.md`。

## File Structure（改动落点）

- `server/app/core/config.py` — **Modify**：加 `AiEngineConfig` 模型、`ai_engines` 改 `list[AiEngineConfig]`、加纯函数 `resolve_engine()`。配置 + 解析的唯一真源。
- `server/app/modules/ai_generation/scheme_router.py` — **Modify**：`/ai-engines` 端点构造方式改为显式取字段（配置项从 dict 变对象后 `AiEngineRead(**e)` 会失效；顺带杜绝密钥泄漏）。
- `server/app/modules/ai_generation/article_writer.py` — **Modify**：写作内核用 `resolve_engine()`、删 `_inject_api_key()`、litellm 调用补 `api_base`。
- `server/tests/test_ai_engine_resolution.py` — **Create**：`resolve_engine()` 纯函数分支测试（无 DB）。
- `server/tests/test_ai_writer_credentials.py` — **Create**：写作内核把正确 model/api_key/api_base 传给 litellm 的集成测试（MySQL）。
- `server/tests/test_generation_schemes.py` — **Modify**：`/ai-engines` 端点不泄漏 `api_key`/`base_url` 的断言。
- `.env.example` — **Modify**：补 `GEO_AI_ENGINES` 示例。
- `CLAUDE.md` — **Modify**：AI 生文模块一节注明「写作引擎含 per-engine key/base_url」。

> ⚠️ 注意：`server/app/modules/ai_generation/pipeline.py` 里**另有一个同名** `_inject_api_key`，属已 410 休眠的旧 LangGraph 流，**不要动它**。本计划只改 `article_writer.py` 里的那个。

---

## Task 1: config.py —— 类型化引擎配置 + `resolve_engine()` + 修端点

**Files:**
- Modify: `server/app/core/config.py`
- Modify: `server/app/modules/ai_generation/scheme_router.py:98`
- Create: `server/tests/test_ai_engine_resolution.py`
- Modify: `server/tests/test_generation_schemes.py`（`test_ai_engines_endpoint_returns_configured_list`）

> 说明：配置项类型一改，`/ai-engines` 端点旧写法 `AiEngineRead(**e)` 立刻失效（不能对 pydantic 对象解包），所以端点修复必须和配置改动同一个 Task 落地，保持测试套件常绿。

- [ ] **Step 1: 写失败测试 —— `resolve_engine` 纯函数分支（无 DB）**

Create `server/tests/test_ai_engine_resolution.py`：

```python
"""resolve_engine 纯函数分支：默认引擎 / 命中带凭据 / 引擎 key 空回落 / 列表外原样。

不依赖 DB，裸 pytest 即可跑。每个用例用 monkeypatch.setenv + cache_clear 隔离配置。
"""

import json

from server.app.core.config import get_settings, resolve_engine


def _set_engines(monkeypatch, engines: list[dict]) -> None:
    monkeypatch.setenv("GEO_AI_MODEL", "default-model")
    monkeypatch.setenv("GEO_AI_API_KEY", "default-key")
    monkeypatch.setenv("GEO_AI_ENGINES", json.dumps(engines))
    get_settings.cache_clear()


def test_resolve_engine_empty_returns_default(monkeypatch):
    _set_engines(monkeypatch, [{"label": "x", "model": "m", "api_key": "k"}])
    try:
        assert resolve_engine("") == ("default-model", "default-key", None)
        assert resolve_engine(None) == ("default-model", "default-key", None)
        assert resolve_engine("   ") == ("default-model", "default-key", None)
    finally:
        get_settings.cache_clear()


def test_resolve_engine_hit_uses_own_credentials(monkeypatch):
    _set_engines(
        monkeypatch,
        [{"label": "DS", "model": "deepseek/deepseek-chat", "api_key": "ds-key",
          "base_url": "https://ds/v1"}],
    )
    try:
        assert resolve_engine("deepseek/deepseek-chat") == (
            "deepseek/deepseek-chat", "ds-key", "https://ds/v1",
        )
    finally:
        get_settings.cache_clear()


def test_resolve_engine_blank_key_falls_back_to_default_key(monkeypatch):
    _set_engines(monkeypatch, [{"label": "C2", "model": "claude-x", "api_key": ""}])
    try:
        assert resolve_engine("claude-x") == ("claude-x", "default-key", None)
    finally:
        get_settings.cache_clear()


def test_resolve_engine_unknown_model_passthrough_with_default_key(monkeypatch):
    _set_engines(monkeypatch, [{"label": "DS", "model": "deepseek/deepseek-chat", "api_key": "ds"}])
    try:
        assert resolve_engine("gpt-foo") == ("gpt-foo", "default-key", None)
    finally:
        get_settings.cache_clear()


def test_ai_engines_parse_credentials_from_json(monkeypatch):
    _set_engines(
        monkeypatch,
        [{"label": "网关", "model": "openai/gpt-4o", "api_key": "gw", "base_url": "https://gw/v1"}],
    )
    try:
        e = get_settings().ai_engines[0]
        assert (e.label, e.model, e.api_key, e.base_url) == (
            "网关", "openai/gpt-4o", "gw", "https://gw/v1",
        )
    finally:
        get_settings.cache_clear()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_ai_engine_resolution.py -q`
Expected: FAIL，`ImportError: cannot import name 'resolve_engine'`。

- [ ] **Step 3: 实现 config.py 改动**

3a. 顶部加 `BaseModel` 导入（现有 import 块）：

```python
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
```

3b. 在 `class Settings(BaseSettings):` **之前**加引擎配置模型：

```python
class AiEngineConfig(BaseModel):
    """单个写作引擎：label + litellm 模型串 + 自带密钥/网关。

    通过 GEO_AI_ENGINES 传 JSON 数组覆盖。下拉选中存的是 model 串，
    后端按 model 串回查本配置拿 api_key / base_url（见 resolve_engine）。
    """

    label: str
    model: str = ""              # "" = 用 settings.ai_model 默认写作模型
    api_key: str = ""            # "" = 回落到 settings.ai_api_key
    base_url: str | None = None  # OpenAI 兼容网关/代理；None = litellm 默认
```

3c. 替换 `Settings` 里 `ai_engines` 那段注释 + 字段（现 config.py:74-78）：

```python
    # 方案级可选 AI 引擎列表（为后续接入更多写作模型留接口）。
    # 每项 = AiEngineConfig（label 展示名 / model litellm 串 / api_key / base_url）。
    # model 空 = 用 ai_model 默认；api_key 空 = 回落 ai_api_key；base_url 留空 = litellm 默认。
    # 通过 GEO_AI_ENGINES 传 JSON 覆盖，例如：
    #   [{"label":"DeepSeek","model":"deepseek/deepseek-chat","api_key":"sk-ds"},
    #    {"label":"网关","model":"openai/gpt-4o","api_key":"sk-x","base_url":"https://oneapi/v1"}]
    ai_engines: list[AiEngineConfig] = [AiEngineConfig(label="默认写作模型")]  # GEO_AI_ENGINES
```

3d. 在 `get_settings()` 函数**之后**加解析纯函数：

```python
def resolve_engine(selected: str | None) -> tuple[str, str, str | None]:
    """下拉选中的 model 串 → 实际调用参数 (model, api_key, base_url)。

    - 空串 / None：系统默认引擎（ai_model + ai_api_key）。
    - 命中 ai_engines 某项：用该项 model（空则默认）、api_key（空则回落默认 key）、base_url。
    - 列表里没有（手填 / 历史值）：原样用该 model + 默认 key。
    """
    settings = get_settings()
    sel = (selected or "").strip()
    if not sel:
        return settings.ai_model, settings.ai_api_key, None
    for e in settings.ai_engines:
        if e.model == sel:
            return e.model or settings.ai_model, e.api_key or settings.ai_api_key, e.base_url
    return sel, settings.ai_api_key, None
```

- [ ] **Step 4: 修 `/ai-engines` 端点**

`server/app/modules/ai_generation/scheme_router.py:98`，把：

```python
    return [AiEngineRead(**e) for e in get_settings().ai_engines]
```

改为（配置项现为对象；显式取字段，绝不铺开整对象，杜绝 api_key 泄漏）：

```python
    return [AiEngineRead(label=e.label, model=e.model) for e in get_settings().ai_engines]
```

- [ ] **Step 5: 加端点不泄漏断言**

`server/tests/test_generation_schemes.py` 的 `test_ai_engines_endpoint_returns_configured_list`，在现有断言后追加：

```python
        # 永不泄漏密钥 / 网关地址给前端
        assert "api_key" not in data[0]
        assert "base_url" not in data[0]
```

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m pytest server/tests/test_ai_engine_resolution.py -q`
Expected: PASS（5 passed）。

Run（需 MySQL，验证端点改造 + 不泄漏；`<url>` 替换为本机测试库）：
`GEO_TEST_DATABASE_URL=<url> python -m pytest "server/tests/test_generation_schemes.py::test_ai_engines_endpoint_returns_configured_list" -q`
Expected: PASS。

- [ ] **Step 7: 静态检查**

Run: `ruff check server/app/core/config.py server/app/modules/ai_generation/scheme_router.py`
Expected: 无错误（若 `ruff format --check` 报格式，跑 `ruff format` 修）。

- [ ] **Step 8: Commit**

```bash
git add server/app/core/config.py server/app/modules/ai_generation/scheme_router.py server/tests/test_ai_engine_resolution.py server/tests/test_generation_schemes.py
git commit -m "feat(ai): 引擎配置带 per-engine key/base_url + resolve_engine 解析

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: 写作内核接 `resolve_engine` —— 凭据随引擎切换

**Files:**
- Modify: `server/app/modules/ai_generation/article_writer.py`
- Create: `server/tests/test_ai_writer_credentials.py`

- [ ] **Step 1: 写失败的集成测试（MySQL）**

Create `server/tests/test_ai_writer_credentials.py`：

```python
"""写作内核 generate_article_from_prompt 把「选中引擎的」model/api_key/api_base
正确传给 litellm。LiteLLM mock，不真实出网。需 MySQL（落 create_article）。
"""

import json
from types import SimpleNamespace

from server.tests.utils import build_test_app


def _fake_completion(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _admin_id(session_factory) -> int:
    from server.app.modules.system.models import User

    with session_factory() as db:
        return db.query(User).first().id


def _run_writer(app, monkeypatch, *, selected_model):
    from server.app.core.config import get_settings
    from server.app.modules.ai_generation.article_writer import generate_article_from_prompt

    seen: dict = {}

    def _cap(**kw):
        seen.update(
            model=kw.get("model"), api_key=kw.get("api_key"), api_base=kw.get("api_base")
        )
        return _fake_completion("# 标题\n\n正文")

    monkeypatch.setattr("litellm.completion", _cap)
    get_settings.cache_clear()
    uid = _admin_id(app.session_factory)
    generate_article_from_prompt(
        session_factory=app.session_factory,
        user_id=uid,
        template_content="写：{{问题}}",
        question_text="1. 问题a1",
        model=selected_model,
    )
    return seen


def test_writer_uses_selected_engine_key_and_base_url(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_AI_MODEL", "default-model")
        monkeypatch.setenv("GEO_AI_API_KEY", "default-key")
        monkeypatch.setenv(
            "GEO_AI_ENGINES",
            json.dumps([{"label": "网关", "model": "openai/gpt-4o", "api_key": "gw-key",
                         "base_url": "https://gw/v1"}]),
        )
        seen = _run_writer(app, monkeypatch, selected_model="openai/gpt-4o")
        assert seen["model"] == "openai/gpt-4o"
        assert seen["api_key"] == "gw-key"
        assert seen["api_base"] == "https://gw/v1"
    finally:
        from server.app.core.config import get_settings

        get_settings.cache_clear()
        app.cleanup()


def test_writer_default_engine_uses_default_key_no_base_url(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_AI_MODEL", "default-model")
        monkeypatch.setenv("GEO_AI_API_KEY", "default-key")
        monkeypatch.setenv("GEO_AI_ENGINES", json.dumps([{"label": "默认", "model": ""}]))
        seen = _run_writer(app, monkeypatch, selected_model=None)
        assert seen["model"] == "default-model"
        assert seen["api_key"] == "default-key"
        assert seen["api_base"] is None
    finally:
        from server.app.core.config import get_settings

        get_settings.cache_clear()
        app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `GEO_TEST_DATABASE_URL=<url> python -m pytest server/tests/test_ai_writer_credentials.py -q`
Expected: FAIL —— `test_writer_uses_selected_engine_key_and_base_url` 断言挂在 `api_key`（旧实现传的是 `default-key`，且没传 `api_base`）。

- [ ] **Step 3: 改写作内核**

`server/app/modules/ai_generation/article_writer.py`：

3a. 删顶部 `import os`（[article_writer.py:9](server/app/modules/ai_generation/article_writer.py#L9)）—— 删 `_inject_api_key` 后它不再被用，留着会触发 ruff F401。

3b. 删整个 `_inject_api_key` 函数（[article_writer.py:44-47](server/app/modules/ai_generation/article_writer.py#L44-L47)）：

```python
def _inject_api_key(api_key: str) -> None:
    if api_key:
        os.environ.setdefault("ANTHROPIC_API_KEY", api_key)
        os.environ.setdefault("OPENAI_API_KEY", api_key)
```

3c. `generate_article_from_prompt` 内部，把 import + 取 settings + 注入 + litellm 调用这一段：

```python
    import litellm

    from server.app.core.config import get_settings
    from server.app.modules.ai_generation.converter import markdown_to_html, markdown_to_tiptap
    from server.app.modules.articles.schemas import ArticleCreate
    from server.app.modules.articles.service import create_article

    settings = get_settings()
    _inject_api_key(settings.ai_api_key)

    user_prompt = (
        render_question_prompt(template_content, question_text)
        + "\n\n请开始写作（只输出 Markdown 正文，含 # 一级标题，不要解释）："
    )
    response = litellm.completion(
        model=(model or "").strip() or settings.ai_model,
        messages=[
            {"role": "system", "content": _GENERIC_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        api_key=settings.ai_api_key or None,
        timeout=300,
        max_tokens=12000,
    )
```

替换为：

```python
    import litellm

    from server.app.core.config import resolve_engine
    from server.app.modules.ai_generation.converter import markdown_to_html, markdown_to_tiptap
    from server.app.modules.articles.schemas import ArticleCreate
    from server.app.modules.articles.service import create_article

    model_str, api_key, base_url = resolve_engine(model)

    user_prompt = (
        render_question_prompt(template_content, question_text)
        + "\n\n请开始写作（只输出 Markdown 正文，含 # 一级标题，不要解释）："
    )
    response = litellm.completion(
        model=model_str,
        messages=[
            {"role": "system", "content": _GENERIC_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        api_key=api_key or None,
        api_base=base_url or None,
        timeout=300,
        max_tokens=12000,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `GEO_TEST_DATABASE_URL=<url> python -m pytest server/tests/test_ai_writer_credentials.py -q`
Expected: PASS（2 passed）。

- [ ] **Step 5: 回归 —— 既有方案运行的引擎透传仍绿**

Run: `GEO_TEST_DATABASE_URL=<url> python -m pytest "server/tests/test_scheme_runs.py::test_run_scheme_passes_ai_engine_model_to_llm" server/tests/test_ai_generation_nodes.py -q`
Expected: PASS（三个生文入口未改、行为不变）。

- [ ] **Step 6: 静态检查**

Run: `ruff check server/app/modules/ai_generation/article_writer.py`
Expected: 无错误（特别确认 `os` 已无未用 import）。

- [ ] **Step 7: Commit**

```bash
git add server/app/modules/ai_generation/article_writer.py server/tests/test_ai_writer_credentials.py
git commit -m "feat(ai): 写作内核按选中引擎切换 key/base_url，删跨厂商串 key 的 _inject_api_key

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: 文档 —— `.env.example` + `CLAUDE.md`

**Files:**
- Modify: `.env.example`
- Modify: `CLAUDE.md`

- [ ] **Step 1: 补 `.env.example` 引擎示例**

`.env.example` 在 `# GEO_AI_API_KEY=`（第 50 行）**之后**、`# ---------- AI 格式调整` 之前插入：

```bash
# 可选写作引擎列表（JSON 数组）。下拉「模型」从这里取；每项自带 api_key/base_url，
# 选哪个就用哪个的凭据，无需改上面的 GEO_AI_MODEL/GEO_AI_API_KEY。
# model 空=用 GEO_AI_MODEL；api_key 空=回落 GEO_AI_API_KEY；base_url 仅 OpenAI 兼容网关需要。
# GEO_AI_ENGINES=[{"label":"Claude","model":"claude-3-5-sonnet-20241022","api_key":"sk-ant-…"},{"label":"DeepSeek","model":"deepseek/deepseek-chat","api_key":"sk-…"},{"label":"公司网关","model":"openai/gpt-4o","api_key":"sk-…","base_url":"https://oneapi.mycorp/v1"}]
```

- [ ] **Step 2: 更新 `CLAUDE.md`**

`CLAUDE.md` 的「AI 生文模块」一节，`GEO_AI_MODEL / GEO_AI_API_KEY — 主写作模型` 这一行**之后**补一条子项：

```markdown
  - 写作模型可在前端下拉切换：候选来自 `GEO_AI_ENGINES`（JSON 数组，每项 `label/model/api_key/base_url`，`api_key` 空则回落 `GEO_AI_API_KEY`）。下拉存 model 串，运行时 `config.resolve_engine()` 回查该引擎的 key/base_url 显式传给 LiteLLM。`AiEngineRead` 只暴露 `label/model`，绝不下发 key。
```

- [ ] **Step 3: Commit**

```bash
git add .env.example CLAUDE.md
git commit -m "docs(ai): GEO_AI_ENGINES per-engine 凭据用法（.env.example + CLAUDE.md）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec 覆盖**
- 配置结构（AiEngineConfig + 列表）→ Task 1 Step 3。✓
- resolve_engine 解析 + 四分支 → Task 1 Step 1/3。✓
- writer 接解析 + api_base + 删 _inject_api_key → Task 2 Step 3。✓
- 三入口零改动（验证）→ Task 2 Step 5。✓
- 防泄漏（AiEngineRead 只 label/model + 测试）→ Task 1 Step 4/5。✓
- 前端零改动 → 无 Task（设计即如此）。✓
- 错误处理（缺 key 让 litellm 自然抛 / JSON 错启动报错）→ 无新代码，resolve_engine 不自造错误（Task 1 实现已体现）。✓
- 测试 1-4 → Task 1（resolve/parse/endpoint-leak）+ Task 2（writer 行为）。✓
- 文档 → Task 3。✓

**2. 占位符扫描**：无 TBD/TODO；每个改码步骤都给了完整代码与确切命令。`<url>` 是运行者本机测试库连接串（CLAUDE.md 已说明 `GEO_TEST_DATABASE_URL`），非占位代码。✓

**3. 类型一致性**：`resolve_engine(selected) -> (model, api_key, base_url)` 在 Task 1 定义、Task 2 解包 `model_str, api_key, base_url` 一致；`AiEngineConfig` 字段 `label/model/api_key/base_url` 在配置、解析、端点、测试中名称一致；端点 `AiEngineRead(label=..., model=...)` 与 `AiEngineRead` 既有字段一致。✓
