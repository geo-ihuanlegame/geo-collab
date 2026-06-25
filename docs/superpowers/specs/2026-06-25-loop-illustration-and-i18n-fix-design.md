# `/goal` Loop 配图对齐 Web UI + 进度日志中文化 · 设计

- 状态：设计稿（v0），待 review 后进入 writing-plans 阶段
- 日期：2026-06-25
- 上游：[`2026-06-24-goal-loop-engineering-design.md`](./2026-06-24-goal-loop-engineering-design.md)（已合 PR #144）+ [`2026-06-24-loop-skill-distribution-design.md`](./2026-06-24-loop-skill-distribution-design.md)（已合 PR #147）
- 受众：实施 plan 评审 + Loop 使用者
- 不动的部分：`/goal` 整体 loop 架构 / netto 验证 / writer-verifier 隔离 / MCP 鉴权
- 动的部分：① 修配图缺失 bug（正文 + 封面都没有），方式 = MCP loop 也走 Web UI 同款的 `run_ai_format`；② 主对话进度日志全中文

---

## 0. 一句话

把 /goal Loop 的配图链路从「按位置硬塞 + 要 stock_categories 配置」改成「调
新 MCP 工具 `ai_illustrate_article` 走 `run_ai_format`」——和 Web UI 「AI 配图」
节点共享同一份 `articles/ai_illustrate_svc.py` service；同时把 orchestrator
skill 主对话进度日志 6 行的英文混搭改成全中文（subagent prompts 保持英文不动）。
一次 PR、一次 bundle version bump（v1 → v2）解决两件事。

---

## 1. 问题定位

### 1.1 配图缺失（高置信，代码定位完成）

| 缺失 | 根因（带代码行号） |
|---|---|
| **正文插图** | writer skill 调 `illustrate_article(article_id)` 不传 `category_ids` → 后端 `articles/router.py:992-1001` 走 `article.stock_categories or []` → 空 → **抛 400 "no category_ids"** → writer skill「失败吞掉」→ 实际 0 张图入库 |
| **封面图** | `cover_asset_id` 只由 `scheme_executor.py:509` 自动选（Web UI「方案运行」走的），MCP `save_from_mcp` 完全不触发——封面字段始终 NULL |

且即使补上 stock_categories，旧的 `illustrate_article_mcp` endpoint 用的是
**简陋按位置塞图**算法（位置写死 `[2, 4, 6]`、不调 AI、3 张图封顶），跟 Web UI
的「AI 配图」效果差一个量级。Web UI 节点 `pipelines/nodes/ai_illustrate.py`
用的是 `articles/ai_format.run_ai_format`——AI 模型按内容决定插哪几张、哪里、
配合主推+陪衬栏目、aggressive/conservative 风格，且**顺手设封面**
（`_maybe_set_cover` 函数）。

### 1.2 进度日志英文混搭

orchestrator skill `进度日志` 段落定义的 6 行格式有英文残留：

| 现状（英文混搭） |
|---|
| `[orchestrator] sanity ✓ pool=<name> N=<N> matrix=<code\|default>` |
| `[round k/3N] qid=<id> → writer …` |
| `[round k/3N] writer 交稿 article_id=<id>, verifier …` |
| `[round k/3N] verifier decision=<d> score=<total>` |
| `[netto] today approved by goal-verifier: <count>/<N>` |
| `[done\|abort] 净产出 <count>/<N>, 共耗时 <m>m, 原因=<...>` |

第 5 行几乎全英文（`today approved by goal-verifier`），其余都掺英文动词
`sanity / writer / verifier`。

---

## 2. 锁定决策（brainstorming 已答）

| # | 决策 | 选项 |
|---|------|------|
| 1 | 中文化范围 | **只改主对话可见的进度日志行**；subagent prompts 保持英文（技术契约，强翻中文反而易误解硬约束如 "ONLY a single-line JSON"） |
| 2 | writer 怎么知道用哪个图库栏目 | **写死在 writer skill 的「矩阵特例」段**：每个矩阵自己填 `default_main_category_id` |
| 3 | 封面图怎么触发 | **`ai_illustrate_article` 同时负责配图 + 设封面**（`set_cover=True` 默认），不用额外加 MCP 工具 |
| 4 | 配图链路 | **对齐 pipeline `ai_illustrate` 节点**：抽取共享 service `articles/ai_illustrate_svc.py`，pipeline 节点 + 新 MCP endpoint 都调它——保证两条路径效果完全一致 |

---

## 3. 架构总览

```
                    ┌────────────────────────────────────────┐
                    │ writer subagent (Claude Code, /goal)    │
                    │                                         │
                    │ save_article(qid, tpl, title, md, ...)  │ ① 落库（不变）
                    │                                         │
                    │ ai_illustrate_article(                  │ ② 新工具
                    │     article_id,                         │
                    │     main_category_id=<from matrix>,     │
                    │ )                                       │
                    └─────────────────┬──────────────────────┘
                                      │
                                      ▼
                  ┌──────────────────────────────────────────┐
                  │ 新增 MCP endpoint                         │
                  │ POST /api/articles/{id}/ai-illustrate    │
                  │ (require_mcp_token)                      │
                  └─────────────────┬───────────────────────┘
                                    │
                                    ▼
        ┌────────────────────────────────────────────────────────┐
        │ 新建共享 service: articles/ai_illustrate_svc.py         │
        │                                                          │
        │ illustrate_one(article_id, main_category_id, options,  │
        │                user_id, session_factory) -> Result      │
        │                                                          │
        │ 内部：                                                    │
        │ • 阶段 1: run_ai_format（AI 决定哪些图哪里）             │
        │   - category_contexts_for(主推 + 陪衬)                   │
        │   - article.ai_checking 加锁                              │
        │ • 阶段 2: set_random_cover_from_category（封面）         │
        │ • 阶段 3: 回读 article.ai_format_error → format_error    │
        └────────────────────────────────────────────────────────┘
                                    ▲
                                    │ 调
                  ┌─────────────────┴───────────────────┐
                  │ 旧 pipeline 节点改用 service          │
                  │ pipelines/nodes/ai_illustrate.py     │
                  │ - `_format_one` + `_maybe_set_cover` │
                  │   被抽走，节点 `_one()` 改 1 行调 service │
                  └─────────────────────────────────────┘
```

### 3.1 关键设计点

1. **抽取共享 service** —— `articles/ai_illustrate_svc.py` 是单文章 illustrate 的
   single source of truth；pipeline 节点 + 新 MCP endpoint **都调它**，两条路径
   配图效果一致
2. **保留旧 `illustrate_article_mcp` endpoint + MCP 工具不动** —— 还有 generation-loop.md
   等旧 Loop 配方在引用；删了破坏向后兼容；将来某个 v3 清
3. **不改 `save_from_mcp`** —— YAGNI；走新工具就不再需要 `article.stock_categories`
   旧路径
4. **错误暴露** —— `run_ai_format` 内部吞异常写到 `article.ai_format_error`；
   service 在阶段 3 回读后挂在 `IllustrateResult.format_error` 返回给调用方
5. **`main_category_id` 由 writer skill 写死** —— 矩阵特例段一行 `default_main_category_id: <id>`；
   fork 矩阵时只改这一个值；首次安装由使用者从 GEO 后台「图库管理」拿
6. **subagent prompts 不动** —— 中文化只覆盖主对话 echo 出来的 6 行进度日志；
   subagent prompt（`Read .claude/skills/.../SKILL.md and follow it strictly`、
   `Output: ONLY a single-line JSON ...`）保持英文以保技术约束清晰度

### 3.2 文件改动

**入库**：

```
server/app/modules/articles/ai_illustrate_svc.py                # 新建（service）
server/app/modules/pipelines/nodes/ai_illustrate.py             # 改用 service
server/app/modules/articles/router.py                            # +ai-illustrate endpoint
server/mcp/tools/action.py                                       # +ai_illustrate_article MCP 工具
server/app/modules/loop_skills/templates/skills/                 # 模板更新
  geo-article-writer/SKILL.md                                    # 矩阵 + step 5 改
  geo-goal-orchestrator/SKILL.md                                 # 6 行日志中文化
server/app/modules/loop_skills/version.py                        # bump v1→v2 + 新 sha
server/tests/test_ai_illustrate_svc.py                           # service 单测（新建）
server/tests/test_articles_ai_illustrate_endpoint.py             # endpoint 测（新建）
docs/superpowers/specs/2026-06-25-loop-illustration-and-i18n-fix-design.md  # 本设计稿
docs/superpowers/plans/2026-06-25-loop-illustration-and-i18n-fix.md         # 实施 plan
```

**不改**：
- `server/app/modules/articles/router.py` 旧 `illustrate_article_mcp` endpoint（保留兼容）
- `server/mcp/tools/action.py` 旧 `illustrate_article` 工具（保留兼容）
- `save_article_from_mcp` 及其 payload
- 任何 articles ORM 模型字段
- 任何前端文件（/goal Loop 是 Claude Code 侧的体验改进，跟 Web UI 无关）

---

## 4. service / endpoint / 工具签名

### 4.1 共享 service：`articles/ai_illustrate_svc.py`（新建）

```python
"""ai_illustrate_svc —— 单篇文章「AI 智能配图 + 自动封面」的共享 service。

被 pipelines/nodes/ai_illustrate.py 和 articles MCP endpoint 共用，
保证两条路径配图效果完全一致。

不并发（单文章），调用方按需自管 ThreadPoolExecutor 包多篇。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from server.app.core.time import utcnow
from server.app.modules.articles.ai_format import (
    category_contexts_for,
    has_ai_format_targets,
    run_ai_format,
)
from server.app.modules.articles.models import Article
from server.app.modules.image_library.cover import (
    CoverResult,
    set_random_cover_from_category,
)

_logger = logging.getLogger(__name__)


@dataclass
class IllustrateOptions:
    """配图旋钮，跟 pipeline ai_illustrate 节点的 cfg 字段一一对应。"""

    include_companion: bool = True       # 主推 + 陪衬都喂给 AI
    web_fallback: bool = False           # AI 兜底 web 抓图
    aggressive_images: bool = True       # 风格：积极=每个游戏都插
    max_images: int | None = None        # None → 风格默认（积极 12 / 保守 3）
    min_spacing: int | None = None       # None → 风格默认（积极 1 / 保守 5）
    preset_id: int | None = None         # 自定义 ai_format 提示词模板
    set_cover: bool = True               # 顺手设封面


@dataclass
class IllustrateResult:
    article_id: int
    images_inserted: int = 0
    cover_status: str = "skipped"        # "set" | "skipped_existing" | "no_image" | "error" | "skipped"
    cover_error: str | None = None
    format_error: str | None = None      # run_ai_format 吞掉的 article.ai_format_error 回读


def illustrate_one(
    *,
    article_id: int,
    main_category_id: int,
    user_id: int,
    options: IllustrateOptions,
    session_factory: Callable[[], Session],
) -> IllustrateResult:
    """给一篇文章配图 + 设封面，复用 pipeline ai_illustrate 节点的成熟逻辑。

    session_factory 而非 db：要开两个独立短 session（配图阶段持锁 / 封面阶段独立提交），
    跟节点里的 `_format_one` + `_maybe_set_cover` 等价。
    """
    aggressive = options.aggressive_images
    builtin_variant = "aggressive" if aggressive else "conservative"
    max_images = options.max_images if (options.max_images and options.max_images > 0) else (
        12 if aggressive else 3
    )
    min_spacing = options.min_spacing if (options.min_spacing and options.min_spacing > 0) else (
        1 if aggressive else 5
    )

    # ─ 阶段 1: 配图（持锁 + run_ai_format）─────────────────────────
    lock_started_at = utcnow().replace(microsecond=0)
    candidate_categories: list = []

    db = session_factory()
    try:
        article = db.get(Article, article_id)
        if article is None or article.is_deleted:
            return IllustrateResult(article_id, format_error="article not found or deleted")
        if not has_ai_format_targets(article.content_json):
            return IllustrateResult(article_id, format_error="no ai_format_targets in content")
        candidate_categories = category_contexts_for(
            db,
            main_category_id=main_category_id,
            include_companion=options.include_companion,
        )
        article.ai_checking = True
        article.ai_checking_started_at = lock_started_at
        article.ai_format_error = None
        db.commit()
    finally:
        db.close()

    images_inserted = run_ai_format(
        article_id,
        include_images=True,
        lock_started_at=lock_started_at,
        preset_id=options.preset_id,
        user_id=user_id,
        candidate_categories=candidate_categories,
        web_fallback=options.web_fallback,
        max_images=max_images,
        min_spacing=min_spacing,
        builtin_variant=builtin_variant,
    )

    # ─ 阶段 2: 封面（独立短 session）──────────────────────────────
    cover_status = "skipped"
    cover_error: str | None = None
    if options.set_cover:
        db = session_factory()
        try:
            article = db.get(Article, article_id)
            if article is not None and not article.is_deleted:
                result: CoverResult = set_random_cover_from_category(
                    db, article, main_category_id, user_id
                )
                db.commit()
                cover_status = result.status
                cover_error = result.error
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            cover_status = "error"
            cover_error = str(exc)
        finally:
            db.close()

    # ─ 阶段 3: 回读 format_error ─────────────────────────────────
    format_error: str | None = None
    db = session_factory()
    try:
        article = db.get(Article, article_id)
        if article is not None:
            format_error = article.ai_format_error
    finally:
        db.close()

    return IllustrateResult(
        article_id=article_id,
        images_inserted=images_inserted or 0,
        cover_status=cover_status,
        cover_error=cover_error,
        format_error=format_error,
    )
```

### 4.2 pipeline 节点改用 service（行为不变）

`pipelines/nodes/ai_illustrate.py` 内：

- 删除 `_format_one()` + `_maybe_set_cover()` 两个内部函数（约 -30 行）
- `_one(article_id)` 改成：

```python
def _one(article_id: int) -> IllustrateResult:
    return illustrate_one(
        article_id=article_id,
        main_category_id=main_category_id,
        user_id=ctx.user_id,
        options=IllustrateOptions(
            include_companion=include_companion,
            web_fallback=web_fallback,
            aggressive_images=aggressive,
            max_images=max_images,
            min_spacing=min_spacing,
            preset_id=effective_preset,
            set_cover=set_cover,
        ),
        session_factory=ctx.session_factory,
    )
```

聚合循环改为消费 `IllustrateResult`：

```python
for fut in as_completed(futures):
    try:
        result = fut.result()
        images_inserted += result.images_inserted
        if result.cover_status == "set":
            covers_set += 1
        elif result.cover_status == "error" and result.cover_error:
            cover_errors.append(f"article {result.article_id}: {result.cover_error}")
        if result.format_error:
            format_errors.append(f"article {result.article_id}: {result.format_error}")
    except Exception as exc:
        errors.append(f"article {futures[fut]}: {exc}")
```

节点对外 NodeResult.output schema **完全不变**（`article_ids` / `errors` /
`images_inserted` / `format_errors` / `covers_set` / `cover_errors` 6 个字段），
保证现有前端 / `agent_run_logs` 展示逻辑零改动。

### 4.3 新 MCP endpoint：`POST /api/articles/{id}/ai-illustrate`

`server/app/modules/articles/router.py` 追加（紧挨现有 `illustrate_article_mcp`）：

```python
class AiIllustratePayload(BaseModel):
    """走 ai_illustrate 节点同款逻辑（AI 决策 + 自动封面）。"""

    main_category_id: int
    include_companion: bool = True
    web_fallback: bool = False
    aggressive_images: bool = True
    max_images: int | None = Field(default=None, ge=1, le=50)
    min_spacing: int | None = Field(default=None, ge=1, le=20)
    preset_id: int | None = None
    set_cover: bool = True


class AiIllustrateResponse(BaseModel):
    images_inserted: int
    cover_status: str            # "set" | "skipped_existing" | "no_image" | "error" | "skipped"
    cover_error: str | None
    format_error: str | None


@articles_mcp_router.post(
    "/{article_id}/ai-illustrate",
    response_model=AiIllustrateResponse,
    dependencies=[Depends(require_mcp_token)],
)
def ai_illustrate_article_mcp(
    article_id: int,
    payload: AiIllustratePayload,
) -> AiIllustrateResponse:
    """[MCP] AI 智能配图 + 自动封面，对齐 Web UI「AI 配图」pipeline 节点。

    复用 articles.ai_illustrate_svc.illustrate_one；与 pipeline 节点共享同一份实现。
    """
    from server.app.db.session import SessionLocal
    from server.app.modules.articles.ai_illustrate_svc import (
        IllustrateOptions,
        illustrate_one,
    )

    # MCP 路径下没有 user JWT，用与 save_from_mcp 同款的 _OPERATOR_USER_ID 常量
    # （来自 GEO_MCP_OPERATOR_USER_ID 环境变量，默认 1）
    result = illustrate_one(
        article_id=article_id,
        main_category_id=payload.main_category_id,
        user_id=_get_mcp_operator_user_id(),
        options=IllustrateOptions(
            include_companion=payload.include_companion,
            web_fallback=payload.web_fallback,
            aggressive_images=payload.aggressive_images,
            max_images=payload.max_images,
            min_spacing=payload.min_spacing,
            preset_id=payload.preset_id,
            set_cover=payload.set_cover,
        ),
        session_factory=SessionLocal,
    )
    return AiIllustrateResponse(
        images_inserted=result.images_inserted,
        cover_status=result.cover_status,
        cover_error=result.cover_error,
        format_error=result.format_error,
    )
```

`_get_mcp_operator_user_id()` helper：实施时从 `server.mcp.config.get_config()`
或环境变量 `GEO_MCP_OPERATOR_USER_ID` 拿（默认 `1`），跟现有 `save_from_mcp` 同款。
若 articles/router.py 没现成符号，从 `server/mcp/tools/action.py` 的
`_OPERATOR_USER_ID` 抄一份。

### 4.4 新 MCP 工具：`server/mcp/tools/action.py` 追加

```python
@mcp.tool()
async def ai_illustrate_article(
    article_id: int,
    main_category_id: int,
    include_companion: bool = True,
    aggressive_images: bool = True,
    set_cover: bool = True,
) -> dict[str, Any]:
    """AI-driven illustration + auto cover for one article (Web UI parity).

    Uses GEO's run_ai_format under the hood — the AI model picks which images
    to insert and where, based on article content. Drops images from
    main_category_id + (optionally) all companion categories. Auto-sets cover
    from main_category_id if article has no cover.

    Args:
        article_id: Target article (must exist).
        main_category_id: Stock image library category id ("主推栏目"). The matrix
            section in your writer SKILL.md tells you which id to use.
        include_companion: If True, also draws from all companion categories.
            Default True matches Web UI default.
        aggressive_images: If True, "积极" style (more images, less spacing).
            Default True matches Web UI default.
        set_cover: If True, also picks a random image from main_category_id
            as the article cover (only if cover not already set).

    Returns:
        {"ok": True, "data": {
            "images_inserted": int,
            "cover_status": str,         # "set" | "skipped_existing" | "no_image" | "error" | "skipped"
            "cover_error": str | None,
            "format_error": str | None,  # surfaced from run_ai_format silent failures
        }, "error": None}
    """
    body: dict[str, Any] = {
        "main_category_id": main_category_id,
        "include_companion": include_companion,
        "aggressive_images": aggressive_images,
        "set_cover": set_cover,
    }
    return await _apost(f"/api/articles/{article_id}/ai-illustrate", json=body)
```

故意只暴露 4 个常用旋钮（main_category_id / include_companion / aggressive_images
/ set_cover），其余高级参数（preset_id / web_fallback / max_images / min_spacing）
走 endpoint 默认值。LLM-facing 工具签名越短越好；想调 advanced 的运维直接 curl
endpoint。

---

## 5. skill 改动

### 5.1 writer skill：`geo-article-writer/SKILL.md`

**Required Checklist step 5 改前/改后**：

```diff
- 5. `illustrate_article(article_id)` — best-effort，失败吞掉
+ 5. `ai_illustrate_article(article_id, main_category_id=<从矩阵特例段拿>)` —
+    AI 智能配图 + 自动封面，**返回值检查 `format_error` / `cover_error` 字段**；
+    有错就在最后 JSON 里加 `illustration_warnings` 透传给 orchestrator，不抛错
```

**矩阵特例段改前/改后**：

```diff
  ## 矩阵特例：餐厅养成记官方矩阵（默认）

  - 风格：轻松实用，避免「开篇一段宏大引入」，直接进主题
  - 偏好题材：游戏推荐 / 攻略 / 玩法解析 / 国风游戏综述
- - 配图类别：温馨治愈、国风山水（具体 stock_category_id 让 illustrate_article
-   自动按文章 tag 选；不要写死 category_ids）
+ - 配图主推栏目：`main_category_id = <REPLACE_ME>`  # ← 安装时查 GEO 后台
+   「图库管理」→ 主推栏目「餐厅养成记」的 id；写死在这里
+ - 配图风格：默认 `aggressive_images=True`（积极配图，每个明确出现的游戏都插）
+ - 封面：默认 `set_cover=True`（从主推栏目随机取一张做封面，已有封面则跳过）
+ - 陪衬：默认 `include_companion=True`（AI 同时从所有陪衬栏目选）
+
+ > 调用约定：
+ > `ai_illustrate_article(article_id=<>, main_category_id=<上面那个值>)`
+ > 其余 3 个布尔参数走默认即可。
```

`<REPLACE_ME>` 由使用者第一次装 skill 后填——README 同步加 onboarding 第 6 步。

### 5.2 orchestrator skill：`geo-goal-orchestrator/SKILL.md`

进度日志格式 6 行映射：

| 现状（英文混搭） | 改成（全中文） |
|---|---|
| `[orchestrator] sanity ✓ pool=<name> N=<N> matrix=<code\|default>` | `[快检] pool=<name> N=<N> matrix=<code\|默认> 通过` |
| `[round k/3N] qid=<id> → writer …` | `[第 k/3N 轮] 选题 qid=<id> → 改写中 …` |
| `[round k/3N] writer 交稿 article_id=<id>, verifier …` | `[第 k/3N 轮] 改写完成 article_id=<id>, 评审中 …` |
| `[round k/3N] verifier decision=<d> score=<total>` | `[第 k/3N 轮] 评审 决策=<d> 总分=<total>` |
| `[netto] today approved by goal-verifier: <count>/<N>` | `[净产出] 今日通过 goal 评审的文章数: <count>/<N>` |
| `[done\|abort] 净产出 <count>/<N>, 共耗时 <m>m, 原因=<...>` | `[完成\|中止] 净产出 <count>/<N>, 共耗时 <m>m, 原因=<...>` |

设计规则：
- 保留方括号 tag（`[快检]` / `[第 k/3N 轮]` 等）方便用户 grep 状态
- 保留英文技术术语 `pool / qid / matrix / article_id / decision / total /
  goal-verifier`——这些是数据库字段名 / 内部标识符，强译反而难对应日志和 DB
- `writer / verifier` 翻成「改写 / 评审」（动作描述可以中文化）
- `sanity ✓` → `快检 … 通过`

subagent prompts（如 `Read .claude/skills/.../SKILL.md and follow it strictly`、
`Output: ONLY a single-line JSON object`）**保持英文不动**——上一题用户已选「只
改主对话可见的进度日志行」。

### 5.3 README 模板（`templates/README.md`）小补丁

onboarding 5 步加 step 6：

```markdown
6. （仅首次）打开本机 `.claude/skills/geo-article-writer/SKILL.md`，找到
   「矩阵特例」段的 `main_category_id = <REPLACE_ME>` 行；去 GEO 后台
   「图库管理」→ 主推栏目里找你矩阵对应栏目（比如餐厅养成记），把 id
   填进去（数字）。
```

---

## 6. bundle version bump

改 `server/app/modules/loop_skills/version.py`：

```python
LOOP_SKILL_BUNDLE_VERSION = "2026-06-25-v2"   # was "2026-06-24-v1"

KNOWN_BUNDLE_SHAS: frozenset[str] = frozenset({
    "49f824a36606c285b84c71ade5aec406ea3c545599b7a3ccf59f863b521667dd",  # v1（保留——本地装过旧版也算 known）
    "<v2 实施时跑 build_bundle 拿到的新 sha>",
})
```

> v1 sha 保留：已经装了 v1 的使用者本机 .claude/ 里就是 v1 内容；他们升级前
> KNOWN 仍要认。Web Section ⑤ 的 `/info` 端点会返回当前 v2 + sha，使用者比对
> 自己本机版本判断是否要重装。

---

## 7. 错误处理 + 不变式

### 7.1 失败矩阵

| 层 | 故障 | service 反应 | endpoint 反应 | writer skill 反应 |
|---|---|---|---|---|
| **article 不存在** | `db.get(Article, ...)` 返 None | 返 `IllustrateResult(format_error="article not found or deleted")` | 200，body 含 format_error | 把 format_error 写进 `illustration_warnings` |
| **article 无 ai_format_targets** | `has_ai_format_targets()` 返 False | 返 `format_error="no ai_format_targets in content"` | 同上 | 同上 |
| **run_ai_format 失败** | 函数自身吞掉异常，写到 `article.ai_format_error` | 阶段 3 回读，挂在 `format_error` | 同上 | 同上 |
| **GEO_AI_FORMAT_API_KEY 未配 / 401 / 5xx** | run_ai_format 抛 → 写到 article.ai_format_error | 同上 | 同上 |
| **封面：cover 已存在** | `set_random_cover_from_category` 返 `CoverResult("skipped_existing")` | `cover_status="skipped_existing"` | 200 | 不报警 |
| **封面：栏目没图** | 返 `CoverResult("no_image")` | `cover_status="no_image"` | 200 | 不报警（也算预期） |
| **封面：MinIO/IO 错** | 返 `CoverResult("error", msg)` | `cover_status="error"`, `cover_error=msg` | 200 | 写进 `illustration_warnings` |
| **service 内部未捕获 exception** | 上抛到 endpoint | endpoint 经 mcp_exception_response 包成 500 | writer skill 看到 MCP 错误，标记本轮失败 |

### 7.2 三个不变式

1. **配图 best-effort**：永远不阻断 article 落库 / 评审流程——`illustrate_one`
   返回错误信息但不抛（除非 service 内部 bug）
2. **pipeline 节点对外 schema 不变**：node output 6 个字段 / `agent_run_logs`
   显示格式完全等价于改造前
3. **MCP 路径 ≡ Web UI 路径**：同一个 service、同一个 `run_ai_format` 调用、
   同一个 `set_random_cover_from_category` 调用——两条 loop 配图效果必须 indistinguishable

---

## 8. 测试策略

### 8.1 自动测（CI 跑）

| 测试 | 文件 | 用例数 |
|---|---|---|
| `illustrate_one` happy path（mock run_ai_format + set_cover 返 set） | `test_ai_illustrate_svc.py`（新建） | 1 |
| `illustrate_one` 无文章/已软删 → 返 format_error="article not found or deleted" | 同上 | 1 |
| `illustrate_one` no ai_format_targets → 返对应 format_error | 同上 | 1 |
| `illustrate_one` cover 失败 → cover_status="error" + cover_error 非空 | 同上 | 1 |
| `illustrate_one` `set_cover=False` 跳过封面阶段 | 同上 | 1 |
| `illustrate_one` 回读 article.ai_format_error → 挂在 format_error | 同上 | 1 |
| pipeline 节点改用 service 后 NodeResult.output 6 字段不变 | `test_pipeline_ai_illustrate.py`（若已存在则改，否则新建） | 1 |
| `POST /api/articles/{id}/ai-illustrate` 无 X-MCP-Token → 401 | `test_articles_ai_illustrate_endpoint.py`（新建） | 1 |
| `POST /api/articles/{id}/ai-illustrate` 带 token + mock service → 200 + 正确 schema | 同上 | 1 |
| Bundle sha v2 在 KNOWN | 既有 `test_loop_skill_bundle.py` | 已有，自动验 |

**有意不测**：
- AI 模型实际选图质量（LLM 输出不稳）
- 真实 MinIO 取图（mock `set_random_cover_from_category` 即可）
- `run_ai_format` 内部逻辑（pipeline 节点测过；此处只测调度）

### 8.2 手工冒烟

| # | 步骤 | 期望 |
|---|---|---|
| 1 | 重装 loop skills v2 到本机 `.claude/`（Web Section ⑤ 「下载 ZIP」或 install_loop_skills MCP 工具） | bundle 版本 `2026-06-25-v2` |
| 2 | 找到主推栏目 id 填到本机 writer skill 矩阵特例段 | 文件保存 |
| 3 | 跑 `/goal 1 篇国风游戏文章作为冒烟` | 进度日志**全中文** |
| 4 | 跑完看 article 详情 | 正文里有插图（>= 1 张）+ 有封面图 |
| 5 | 看 verifier 决策 + decision 入库 | 跟 v1 行为一致 |
| 6 | 跑 Web UI 的「方案运行」流程同题材 1 篇 | 配图效果与第 4 步**视觉一致**（数量级、风格） |

---

## 9. 工作量估算 + 实施顺序

### 9.1 工作量

| 模块 | 改动行 | 工时 |
|---|---|---|
| `articles/ai_illustrate_svc.py`（新建） | +130 | 1.5 h |
| `pipelines/nodes/ai_illustrate.py`（改用 service） | -30/+15 | 0.5 h |
| `articles/router.py`（加 ai-illustrate endpoint + helper） | +50 | 0.5 h |
| `mcp/tools/action.py`（加工具） | +40 | 0.3 h |
| `loop_skills/templates/skills/geo-article-writer/SKILL.md`（矩阵 + step 5） | +15/-5 | 0.3 h |
| `loop_skills/templates/skills/geo-goal-orchestrator/SKILL.md`（6 行日志中文化） | +6/-6 | 0.3 h |
| `loop_skills/templates/README.md`（onboarding step 6） | +5 | 0.1 h |
| `loop_skills/version.py`（bump） | +2/-1 | 0.05 h |
| 3 个新测试文件 | +280 | 2 h |
| 现有 pipeline 节点测试 snapshot 校准（如有） | +5/-5 | 0.3 h |
| 手工冒烟 + 调通 | — | 1 h |
| **合计** | **~540 行** | **~6.5 h（约 1 天）** |

### 9.2 实施顺序（依赖关系）

```
1. articles/ai_illustrate_svc.py（独立，TDD）
   ├ IllustrateOptions / IllustrateResult dataclass
   ├ illustrate_one 实现
   └ 6 个单测覆盖所有失败矩阵

2. pipelines/nodes/ai_illustrate.py（改用 service）
   ├ _one() 改成调 service
   ├ 删 _format_one + _maybe_set_cover
   └ snapshot 测试确认 NodeResult.output 不变

3. articles/router.py + mcp/tools/action.py（新 endpoint + 工具）
   ├ AiIllustratePayload / AiIllustrateResponse Pydantic
   ├ ai_illustrate_article_mcp endpoint
   ├ 401 + 200 集成测试
   └ MCP 工具 wrapper（自检 import OK）

4. loop_skills/templates/*（skill / README 改动 + bump 版本）
   ├ geo-article-writer/SKILL.md 矩阵 + step 5
   ├ geo-goal-orchestrator/SKILL.md 6 行日志
   ├ templates/README.md step 6
   ├ version.py bump v1→v2
   └ 跑 test_bundle_sha_is_known 拿新 sha 填进 KNOWN

5. push + PR
   ├ 后端 ruff/format/pytest 全过
   ├ Web UI typecheck（无改动应自然过）
   └ PR description 列冒烟 6 步
```

---

## 10. 与已有 spec / 实现的关系

| 参考 | 关系 |
|---|---|
| [`2026-06-24-goal-loop-engineering-design.md`](./2026-06-24-goal-loop-engineering-design.md) | /goal Loop 主架构（已合 PR #144），本设计是它的 bug fix + i18n |
| [`2026-06-24-loop-skill-distribution-design.md`](./2026-06-24-loop-skill-distribution-design.md) | skill 分发通道（已合 PR #147），本设计 bump bundle version 复用其分发机制 |
| `pipelines/nodes/ai_illustrate.py` | Web UI「AI 配图」节点，本设计抽取其内部函数到共享 service，节点改用 service |
| `articles/ai_format.py` | `run_ai_format` 实现，本设计不动它，只在 service 里调 |
| `image_library/cover.py` | `set_random_cover_from_category`，本设计不动它，只在 service 里调 |

---

## 11. Out of Scope（明确不做的）

- **删除旧 `illustrate_article` MCP 工具 + endpoint**：保留兼容；待 v3 清
- **改 `save_from_mcp` 加 stock_category_ids 字段**：YAGNI，走新工具不再需要
- **暴露 advanced 参数给 LLM 工具**（preset_id / web_fallback / max_images /
  min_spacing）：默认值已经合理；想调直接 curl endpoint
- **多语言 skill 模板**：本设计中文化只覆盖 6 行 echo；未来如要英文版用户群再加
- **/distribute Loop / /weekly-report Loop 的 i18n**：本设计只覆盖 /goal
- **图库栏目 id 「Web 一键复制」按钮**：让使用者手填即可，YAGNI

---

## 12. Smoke Test 与上线门禁

- 后端 ruff / format / mypy / pytest 全过
- 9 个新单测 + 1 个 pipeline snapshot 测通过
- §8.2 手工冒烟 6 步全过（重点：第 4 步看到插图 + 封面，第 6 步与 Web UI 视觉一致）
- 至少 1 个非作者同事按新版 README onboarding 6 步装好 + 跑通 `/goal` 出图
