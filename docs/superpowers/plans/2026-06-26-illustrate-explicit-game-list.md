# 配图：显式游戏清单驱动落图 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 AI 配图在拿到上游显式游戏清单时，按游戏名匹配文章 heading、确定性落图（不调弱 LLM），并修掉 partial_images 计数盲区与 `_derive_html_and_text` 有损渲染。

**Architecture:** 解耦"识别"（上游强模型分支，非本计划）与"落图"（本计划）。新增确定性路径：段1 `_ai_format_prepare` → resolver 把游戏清单合成 `parsed`（`{"image_positions":[...]}`）→ 复用现有 `_web_fallback_collect_and_write_back`（传 `heading_indices=set()`、不提升标题）落图 → 用 `len(清单)` 修正计数。缺省（无清单）走现有 `run_ai_format` 不变。

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy；后端测试 pytest（纯函数测试无需 DB；集成测试用 `build_test_app` + `@pytest.mark.mysql`，需 `GEO_TEST_DATABASE_URL`）。

## Global Constraints

- 配置/运行：`conda activate geo_xzpt`；后端测试 `GEO_TEST_DATABASE_URL=mysql+pymysql://.../geo_test pytest server/tests/ -q`（DB 名须含 "test"）。纯函数测试可裸跑 `pytest server/tests/<f>.py -q`（无 DB 自动可跑）。
- 三份正文（`content_json` / `content_html` / `plain_text`）改任一份要同步另外两份——本计划 Fix-3 即修同步函数。
- service 层抛命名异常（`ClientError`/`ValidationError`/`ConflictError`），不抛裸 `ValueError`。
- 向后兼容铁律：`game_list=None`（缺省）必须走现有 `run_ai_format`，行为零变化。
- 契约语义：清单一项 game = 至多配一张图；同一游戏多个 heading 只配首个。
- lint/format/type：`ruff check server/`、`ruff format server/`、`mypy server/app` 必须过（CI 硬门禁）。
- 提交信息风格：`feat(ai_format): ...` / `fix(ai_format): ...` / `test(...): ...`，结尾带 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。

## File Structure

- **Modify** `server/app/modules/articles/ai_format.py`
  - 重写 `_node_html`（:425-441）→ 递归 + 全 marks + hardBreak。
  - 扩展 `_derive_html_and_text`（:444-456）→ list/image/blockquote/codeBlock + plain_text。
  - 新增 `_normalize_game_name` / `_find_heading_index` / `build_image_positions_from_game_list`（resolver 纯函数）。
  - 新增 `run_ai_format_from_game_list(...)`（确定性无 LLM 编排）。
- **Modify** `server/app/modules/articles/ai_illustrate_svc.py`
  - `IllustrateOptions` 加 `game_list: list[dict] | None = None`。
  - `illustrate_one` 阶段1后按 `game_list` 分叉（有→新路径、无→现状）。
- **Modify** `server/app/modules/articles/router.py`
  - `AiIllustratePayload` 加 `game_positions: list[dict] | None = None`，透传到 `IllustrateOptions.game_list`。
- **Modify** `server/mcp/tools/action.py`
  - `ai_illustrate_article` 工具加可选 `game_positions` 参数，进 body。
- **Create** `server/tests/test_illustrate_render_fix.py`（Fix-3 纯函数测试）
- **Create** `server/tests/test_illustrate_game_list_resolver.py`（resolver 纯函数测试）
- **Create** `server/tests/test_illustrate_game_list_endpoint.py`（端点集成测试，`@pytest.mark.mysql`）

> 现有可复用函数（**不改签名**，仅调用）：
> - `_ai_format_prepare(article_id, *, lock_started_at, include_images, preset_id, user_id, candidate_categories, max_images, min_spacing, builtin_variant, web_fallback) -> _AiFormatPrep | None`（`_AiFormatPrep` 字段：`content_json, available_categories, image_search_query, ...`）
> - `_web_fallback_collect_and_write_back(article_id, *, lock_started_at, new_content_json, parsed, available_categories, heading_indices, image_search_query, max_images, out_diagnostics=None) -> int`
> - `_node_text(node) -> str`、`run_ai_format(...)`（缺省路径）。

---

### Task 1: Fix-3 — 无损 html / plain_text 渲染（独立，先做）

**Files:**
- Modify: `server/app/modules/articles/ai_format.py:425-456`（`_node_html`、`_derive_html_and_text`）
- Test: `server/tests/test_illustrate_render_fix.py`

**Interfaces:**
- Consumes: 无（纯函数，输入 Tiptap content_json dict）。
- Produces: `_derive_html_and_text(content_json: dict) -> tuple[str, str]`（html, plain_text）——签名不变，行为变为保留 list/image/blockquote/codeBlock/marks/hardBreak。

- [ ] **Step 1: 写失败测试**

新建 `server/tests/test_illustrate_render_fix.py`：

```python
from server.app.modules.articles.ai_format import _derive_html_and_text, _node_html


def _doc(*nodes):
    return {"type": "doc", "content": list(nodes)}


def test_bulletlist_preserved_in_html_and_text():
    doc = _doc(
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "游戏一、《餐厅养成记》"}]},
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "平台", "marks": [{"type": "bold"}]},
                    {"type": "text", "text": "：全渠道"},
                ]}
            ]},
        ]},
    )
    html, text = _derive_html_and_text(doc)
    assert "<ul>" in html and "<li>" in html
    assert "<strong>平台</strong>" in html
    assert "全渠道" in html
    assert "平台：全渠道" in text


def test_image_node_preserved_in_html():
    doc = _doc(
        {"type": "paragraph", "content": [{"type": "text", "text": "正文"}]},
        {"type": "image", "attrs": {"src": "/api/stock-images/816/file", "alt": "封面"}},
    )
    html, _ = _derive_html_and_text(doc)
    assert "<img" in html and "/api/stock-images/816/file" in html


def test_marks_and_hardbreak():
    doc = _doc({"type": "paragraph", "content": [
        {"type": "text", "text": "斜", "marks": [{"type": "italic"}]},
        {"type": "hardBreak"},
        {"type": "text", "text": "链", "marks": [{"type": "link", "attrs": {"href": "https://x.com"}}]},
    ]})
    html, _ = _derive_html_and_text(doc)
    assert "<em>斜</em>" in html
    assert "<br>" in html
    assert '<a href="https://x.com">链</a>' in html


def test_ordered_blockquote_codeblock():
    doc = _doc(
        {"type": "orderedList", "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "一"}]}]},
        ]},
        {"type": "blockquote", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "引"}]}]},
        {"type": "codeBlock", "content": [{"type": "text", "text": "code()"}]},
    )
    html, text = _derive_html_and_text(doc)
    assert "<ol>" in html and "<blockquote>" in html and "<pre><code>" in html
    assert "一" in text and "引" in text and "code()" in text
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest server/tests/test_illustrate_render_fix.py -q`
Expected: FAIL（当前 `_derive_html_and_text` 丢 bulletList/image/ol/blockquote/codeBlock，`_node_html` 只认 bold、无 hardBreak/link）。

- [ ] **Step 3: 重写 `_node_html` 与 `_derive_html_and_text`**

把 `ai_format.py:425-456` 两个函数整体替换为：

```python
_INLINE_MARK_TAGS = {
    "bold": ("<strong>", "</strong>"),
    "italic": ("<em>", "</em>"),
    "code": ("<code>", "</code>"),
    "underline": ("<u>", "</u>"),
    "strike": ("<s>", "</s>"),
}


def _inline_html(children: list | None) -> str:
    """渲染一组 inline 子节点（text/hardBreak）为 HTML，保留 marks。与既有风格一致：text 不转义。"""
    parts: list[str] = []
    for child in children or []:
        if not isinstance(child, dict):
            continue
        ctype = child.get("type")
        if ctype == "hardBreak":
            parts.append("<br>")
            continue
        if ctype != "text":
            continue
        text = child.get("text", "")
        for mark in child.get("marks") or []:
            if not isinstance(mark, dict):
                continue
            mtype = mark.get("type")
            if mtype == "link":
                href = (mark.get("attrs") or {}).get("href", "")
                text = f'<a href="{href}">{text}</a>'
            elif mtype in _INLINE_MARK_TAGS:
                open_tag, close_tag = _INLINE_MARK_TAGS[mtype]
                text = f"{open_tag}{text}{close_tag}"
        parts.append(text)
    return "".join(parts)


def _node_html(node: dict) -> str:
    """单个块节点 → HTML。递归处理列表/引用/列表项内的块子节点。"""
    ntype = node.get("type")
    if ntype == "heading":
        level = (node.get("attrs") or {}).get("level", 1)
        return f"<h{level}>{_inline_html(node.get('content'))}</h{level}>"
    if ntype == "paragraph":
        return f"<p>{_inline_html(node.get('content'))}</p>"
    if ntype == "image":
        attrs = node.get("attrs") or {}
        src = attrs.get("src", "")
        alt = attrs.get("alt", "") or ""
        return f'<img src="{src}" alt="{alt}">'
    if ntype in ("bulletList", "orderedList"):
        tag = "ul" if ntype == "bulletList" else "ol"
        items = "".join(_node_html(c) for c in node.get("content") or [] if isinstance(c, dict))
        return f"<{tag}>{items}</{tag}>"
    if ntype == "listItem":
        return f"<li>{''.join(_node_html(c) for c in node.get('content') or [] if isinstance(c, dict))}</li>"
    if ntype == "blockquote":
        return f"<blockquote>{''.join(_node_html(c) for c in node.get('content') or [] if isinstance(c, dict))}</blockquote>"
    if ntype == "codeBlock":
        return f"<pre><code>{_inline_html(node.get('content'))}</code></pre>"
    # 未知块节点：尽量取 inline 文本，不静默吞整块
    return f"<p>{_inline_html(node.get('content'))}</p>"


def _node_plain_text(node: dict) -> str:
    """单个块节点 → 纯文本（递归）。列表项各成一行。"""
    ntype = node.get("type")
    if ntype in ("heading", "paragraph", "codeBlock"):
        return _node_text(node)
    if ntype in ("bulletList", "orderedList", "blockquote", "listItem"):
        lines = [_node_plain_text(c) for c in node.get("content") or [] if isinstance(c, dict)]
        return "\n".join(t for t in lines if t.strip())
    if ntype == "image":
        return ""
    return _node_text(node)


def _derive_html_and_text(content_json: dict) -> tuple[str, str]:
    html_parts: list[str] = []
    text_parts: list[str] = []
    for node in content_json.get("content") or []:
        if not isinstance(node, dict):
            continue
        html_parts.append(_node_html(node))
        t = _node_plain_text(node)
        if t.strip():
            text_parts.append(t)
    return "".join(html_parts), "\n".join(text_parts)
```

> 注：`_node_text`（:156）已有，处理 text+hardBreak→`\n`，保持引用不动。新增 `_node_plain_text` 负责块级递归。

- [ ] **Step 4: 运行确认通过**

Run: `pytest server/tests/test_illustrate_render_fix.py -q`
Expected: PASS（4 用例全绿）。

- [ ] **Step 5: 回归既有 ai_format 测试 + lint**

Run: `ruff check server/app/modules/articles/ai_format.py && ruff format server/app/modules/articles/ai_format.py`
Run: `pytest server/tests/test_ai_format.py -q`（无 DB 用例应仍绿；标 mysql 的会自动跳过）
Expected: lint 干净；既有渲染相关断言不回归。

- [ ] **Step 6: 提交**

```bash
git add server/app/modules/articles/ai_format.py server/tests/test_illustrate_render_fix.py
git commit -m "fix(ai_format): _derive_html_and_text 保留 list/image/marks/hardBreak（无损回写）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: 游戏清单 → 合成 image_positions resolver（纯函数，独立）

**Files:**
- Modify: `server/app/modules/articles/ai_format.py`（在 `_node_text` 附近新增 3 个纯函数）
- Test: `server/tests/test_illustrate_game_list_resolver.py`

**Interfaces:**
- Consumes: `_node_text`（既有）。
- Produces:
  - `build_image_positions_from_game_list(content_json: dict, game_list: list[dict]) -> tuple[list[dict], list[dict]]`
    返回 `(positions, unmatched)`：`positions` = `[{"index": int, "game": str, "category_id"?: int}]`（喂进合成 parsed）；`unmatched` = `[{"game": str, "reason": str}]`（reason ∈ `heading_not_found`/`index_conflict`）。

- [ ] **Step 1: 写失败测试**

新建 `server/tests/test_illustrate_game_list_resolver.py`：

```python
from server.app.modules.articles.ai_format import (
    _normalize_game_name,
    build_image_positions_from_game_list,
)


def _doc(*headings):
    content = []
    for h in headings:
        content.append({"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": h}]})
        content.append({"type": "bulletList", "content": []})  # 制造 index 空洞
    return {"type": "doc", "content": content}


def test_normalize_strips_brackets_and_prefix():
    assert _normalize_game_name("游戏一、《餐厅养成记》") == "餐厅养成记"
    assert _normalize_game_name("《原神》") == "原神"
    assert _normalize_game_name("游戏10、明日方舟") == "明日方舟"


def test_match_by_name_computes_absolute_index_with_gaps():
    doc = _doc("游戏一、《餐厅养成记》", "游戏二、《原神》")  # heading 在 index 0、2
    positions, unmatched = build_image_positions_from_game_list(
        doc, [{"game": "餐厅养成记"}, {"game": "原神"}]
    )
    assert {p["index"] for p in positions} == {0, 2}
    assert unmatched == []


def test_unmatched_game_reported():
    doc = _doc("游戏一、《餐厅养成记》")
    positions, unmatched = build_image_positions_from_game_list(doc, [{"game": "不存在的游戏"}])
    assert positions == []
    assert unmatched == [{"game": "不存在的游戏", "reason": "heading_not_found"}]


def test_index_hint_fallback_when_not_found():
    doc = _doc("游戏一、《餐厅养成记》")
    positions, unmatched = build_image_positions_from_game_list(
        doc, [{"game": "外部游戏", "index": 0}]
    )
    assert positions == [{"index": 0, "game": "外部游戏"}]
    assert unmatched == []


def test_index_conflict_dedup_keeps_first():
    # 同一 heading 文本含两个游戏名 → 解析到同一 index → 去重
    doc = {"type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "原神 与 崩坏"}]},
    ]}
    positions, unmatched = build_image_positions_from_game_list(
        doc, [{"game": "原神"}, {"game": "崩坏"}]
    )
    assert positions == [{"index": 0, "game": "原神"}]
    assert unmatched == [{"game": "崩坏", "reason": "index_conflict"}]


def test_category_id_passthrough():
    doc = _doc("游戏一、《餐厅养成记》")
    positions, _ = build_image_positions_from_game_list(
        doc, [{"game": "餐厅养成记", "category_id": 12}]
    )
    assert positions == [{"index": 0, "game": "餐厅养成记", "category_id": 12}]
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest server/tests/test_illustrate_game_list_resolver.py -q`
Expected: FAIL（`_normalize_game_name` / `build_image_positions_from_game_list` 未定义）。

- [ ] **Step 3: 实现 resolver**

在 `ai_format.py` 的 `_node_text`（:156-165）之后新增：

```python
import re as _re  # 若文件顶部已 import re，则复用，不要重复 import

_GAME_PREFIX_RE = _re.compile(r"^游戏[0-9一二三四五六七八九十百]+、\s*")
_BRACKET_CHARS = "《》〈〉「」『』\"'“”‘’ \t　"


def _normalize_game_name(s: str) -> str:
    """归一化游戏名/heading 文本：去『游戏N、』前缀、去书名号/引号/空白。用于 contains 匹配。"""
    t = (s or "").strip()
    t = _GAME_PREFIX_RE.sub("", t)
    return t.strip(_BRACKET_CHARS)


def _find_heading_index(content_json: dict, game: str) -> int | None:
    """在顶层 heading 节点里找文本含 game 的，返回其绝对下标；多命中取首个；无则 None。"""
    target = _normalize_game_name(game)
    if not target:
        return None
    for i, node in enumerate(content_json.get("content") or []):
        if not isinstance(node, dict) or node.get("type") != "heading":
            continue
        if target in _normalize_game_name(_node_text(node)):
            return i
    return None


def build_image_positions_from_game_list(
    content_json: dict, game_list: list[dict]
) -> tuple[list[dict], list[dict]]:
    """游戏清单 → (合成 image_positions, unmatched)。game 名为权威锚点，index 仅未命中时兜底。

    - 同一 game 命中多个 heading：取首个。
    - 多个 game 解析到同一 index：按 index 去重，保留先到的，其余记 index_conflict。
    - 命中不到 heading 且无 index 提示：记 heading_not_found。
    """
    positions: list[dict] = []
    unmatched: list[dict] = []
    used_index: set[int] = set()
    for item in game_list or []:
        if not isinstance(item, dict):
            continue
        game = (item.get("game") or "").strip()
        if not game:
            continue
        idx = _find_heading_index(content_json, game)
        if idx is None:
            hint = item.get("index")
            if isinstance(hint, int):
                idx = hint
            else:
                unmatched.append({"game": game, "reason": "heading_not_found"})
                continue
        if idx in used_index:
            unmatched.append({"game": game, "reason": "index_conflict"})
            continue
        used_index.add(idx)
        pos: dict = {"index": idx, "game": game}
        cat = item.get("category_id")
        if isinstance(cat, int):
            pos["category_id"] = cat
        positions.append(pos)
    return positions, unmatched
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest server/tests/test_illustrate_game_list_resolver.py -q`
Expected: PASS（6 用例全绿）。

- [ ] **Step 5: lint + 提交**

```bash
ruff check server/app/modules/articles/ai_format.py && ruff format server/app/modules/articles/ai_format.py
git add server/app/modules/articles/ai_format.py server/tests/test_illustrate_game_list_resolver.py
git commit -m "feat(ai_format): 游戏清单→合成 image_positions resolver（名字锚定 heading + index 去重）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: 确定性无 LLM 编排 `run_ai_format_from_game_list`

**Files:**
- Modify: `server/app/modules/articles/ai_format.py`（在 `run_ai_format` 之后新增）
- Test: `server/tests/test_illustrate_game_list_resolver.py`（追加 1 个 monkeypatch 单测，验证 parsed 构造 + 计数修正，不碰 DB）

**Interfaces:**
- Consumes: `_ai_format_prepare(...)`、`build_image_positions_from_game_list(...)`、`_web_fallback_collect_and_write_back(...)`（均既有/上一 Task）。
- Produces:
  - `run_ai_format_from_game_list(article_id: int, *, lock_started_at, game_list: list[dict], preset_id, user_id, candidate_categories, max_images, min_spacing, builtin_variant, out_diagnostics=None) -> int`
    返回 inserted 张数；把 `requested=len(game_list)`、`inserted`、`missed`、`missed_games`（含 unmatched）写入 `out_diagnostics`。

- [ ] **Step 1: 写失败测试（monkeypatch，不碰 DB）**

向 `server/tests/test_illustrate_game_list_resolver.py` 追加：

```python
def test_run_from_game_list_counts_unmatched_as_missed(monkeypatch):
    import server.app.modules.articles.ai_format as m

    class _Prep:
        content_json = {"type": "doc", "content": [
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "游戏一、《餐厅养成记》"}]},
        ]}
        available_categories = []
        image_search_query = None

    monkeypatch.setattr(m, "_ai_format_prepare", lambda *a, **k: _Prep())

    captured = {}

    def _fake_collect(article_id, *, parsed, out_diagnostics=None, **kw):
        captured["parsed"] = parsed
        captured["heading_indices"] = kw.get("heading_indices")
        if out_diagnostics is not None:
            out_diagnostics["inserted"] = 1
            out_diagnostics["missed_games"] = []
        return 1

    monkeypatch.setattr(m, "_web_fallback_collect_and_write_back", _fake_collect)

    diag: dict = {}
    inserted = m.run_ai_format_from_game_list(
        1,
        lock_started_at=None,
        game_list=[{"game": "餐厅养成记"}, {"game": "查无此游戏"}],
        preset_id=None,
        user_id=1,
        candidate_categories=None,
        max_images=12,
        min_spacing=1,
        builtin_variant="aggressive",
        out_diagnostics=diag,
    )
    assert inserted == 1
    # 合成 parsed 只含命中的那一个
    assert captured["parsed"]["image_positions"] == [{"index": 0, "game": "餐厅养成记"}]
    # 不提升标题
    assert captured["heading_indices"] == set()
    # 计数：expected=2、inserted=1、missed=1、missed_games 含未命中的游戏
    assert diag["requested"] == 2
    assert diag["inserted"] == 1
    assert diag["missed"] == 1
    assert "查无此游戏" in diag["missed_games"]
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest server/tests/test_illustrate_game_list_resolver.py::test_run_from_game_list_counts_unmatched_as_missed -q`
Expected: FAIL（`run_ai_format_from_game_list` 未定义）。

- [ ] **Step 3: 实现编排函数**

在 `ai_format.py` 的 `run_ai_format` 定义之后新增：

```python
def run_ai_format_from_game_list(
    article_id: int,
    *,
    lock_started_at: datetime | None,
    game_list: list[dict],
    preset_id: int | None,
    user_id: int | None,
    candidate_categories: list[dict[str, Any]] | None,
    max_images: int | None,
    min_spacing: int | None,
    builtin_variant: str,
    out_diagnostics: dict[str, Any] | None = None,
) -> int:
    """确定性配图：拿显式游戏清单落图，不调 ai_format LLM、不提升标题。

    段1 prepare（取 content_json/栏目/搜图模板，复用现有锁检查）→ resolver 合成 parsed →
    复用 _web_fallback_collect_and_write_back（heading_indices=set()）落图 → 用 len(清单) 修正计数。
    """
    try:
        prep = _ai_format_prepare(
            article_id,
            lock_started_at=lock_started_at,
            include_images=True,
            preset_id=preset_id,
            user_id=user_id,
            candidate_categories=candidate_categories,
            max_images=max_images,
            min_spacing=min_spacing,
            builtin_variant=builtin_variant,
            web_fallback=True,
        )
    except Exception as exc:
        _ai_format_finalize_error(article_id, lock_started_at, exc)
        return 0
    if prep is None:
        return 0

    positions, unmatched = build_image_positions_from_game_list(prep.content_json, game_list)
    parsed = {"image_positions": positions}

    fmt_diag: dict[str, Any] = {}
    try:
        inserted = _web_fallback_collect_and_write_back(
            article_id,
            lock_started_at=lock_started_at,
            new_content_json=prep.content_json,  # 不提升标题：原样
            parsed=parsed,
            available_categories=prep.available_categories,
            heading_indices=set(),
            image_search_query=prep.image_search_query,
            max_images=max_images,
            out_diagnostics=fmt_diag,
        )
    except Exception as exc:
        _ai_format_finalize_error(article_id, lock_started_at, exc)
        return 0

    if out_diagnostics is not None:
        expected = len([g for g in (game_list or []) if isinstance(g, dict) and (g.get("game") or "").strip()])
        out_diagnostics["requested"] = expected
        out_diagnostics["inserted"] = inserted
        out_diagnostics["missed"] = max(0, expected - inserted)
        missed_games = list(fmt_diag.get("missed_games", []) or [])
        missed_games += [u["game"] for u in unmatched]
        out_diagnostics["missed_games"] = missed_games
    return inserted
```

> 说明：`_ai_format_finalize_error`、`_AiFormatPrep`、`datetime`、`Any` 均已在文件内可用。`prep` 的 `system_prompt` 不用（不调 LLM），轻微浪费可接受。

- [ ] **Step 4: 运行确认通过**

Run: `pytest server/tests/test_illustrate_game_list_resolver.py -q`
Expected: PASS（含新单测）。

- [ ] **Step 5: lint + mypy + 提交**

```bash
ruff check server/app/modules/articles/ai_format.py && ruff format server/app/modules/articles/ai_format.py && mypy server/app/modules/articles/ai_format.py
git add server/app/modules/articles/ai_format.py server/tests/test_illustrate_game_list_resolver.py
git commit -m "feat(ai_format): run_ai_format_from_game_list 确定性落图（无 LLM、不提升标题、计数取清单长度）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 接入 `IllustrateOptions` + `illustrate_one` 分叉

**Files:**
- Modify: `server/app/modules/articles/ai_illustrate_svc.py`（`IllustrateOptions` 加字段；`illustrate_one` 分叉）
- Test: 复用 Task 5 的端点集成测试覆盖（本 Task 仅接线，单测在 Task 5）

**Interfaces:**
- Consumes: `run_ai_format_from_game_list(...)`（Task 3）、`run_ai_format(...)`（既有）。
- Produces: `IllustrateOptions.game_list: list[dict] | None`；`illustrate_one` 在有清单时走确定性路径。

- [ ] **Step 1: 给 `IllustrateOptions` 加字段**

`ai_illustrate_svc.py` 的 `IllustrateOptions`（dataclass）加：

```python
    game_list: list[dict] | None = None  # 上游识别分支传下来的显式游戏清单；None=回退现有模型路径
```

- [ ] **Step 2: `illustrate_one` 阶段2分叉**

把 `ai_illustrate_svc.py` 中阶段2调用 `run_ai_format(...)` 处（约 :170-182）改为分叉。先 import 新函数（与现有 import 同处）：

```python
from server.app.modules.articles.ai_format import (
    category_contexts_for,
    has_ai_format_targets,
    run_ai_format,
    run_ai_format_from_game_list,
)
```

阶段2：

```python
    fmt_diag: dict = {}
    if options.game_list is not None:
        images_inserted = run_ai_format_from_game_list(
            article_id,
            lock_started_at=lock_started_at,
            game_list=options.game_list,
            preset_id=options.preset_id,
            user_id=user_id,
            candidate_categories=candidate_categories,
            max_images=max_images,
            min_spacing=min_spacing,
            builtin_variant=builtin_variant,
            out_diagnostics=fmt_diag,
        )
    else:
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
            out_diagnostics=fmt_diag,
        )
```

- [ ] **Step 3: lint + mypy**

Run: `ruff check server/app/modules/articles/ai_illustrate_svc.py && ruff format server/app/modules/articles/ai_illustrate_svc.py && mypy server/app/modules/articles/ai_illustrate_svc.py`
Expected: 干净。

- [ ] **Step 4: 提交**

```bash
git add server/app/modules/articles/ai_illustrate_svc.py
git commit -m "feat(ai_illustrate): IllustrateOptions.game_list + illustrate_one 分叉到确定性路径

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 接入 MCP 端点 / 工具参数 + 集成测试

**Files:**
- Modify: `server/app/modules/articles/router.py`（`AiIllustratePayload` 加 `game_positions`；透传）
- Modify: `server/mcp/tools/action.py:275-339`（工具加 `game_positions` 参数进 body）
- Test: `server/tests/test_illustrate_game_list_endpoint.py`（`@pytest.mark.mysql`）

**Interfaces:**
- Consumes: `IllustrateOptions.game_list`（Task 4）。
- Produces: `POST /api/articles/{id}/ai-illustrate` 接受可选 `game_positions: list[dict]`；MCP 工具 `ai_illustrate_article(..., game_positions=None)`。

- [ ] **Step 1: 写失败的集成测试**

新建 `server/tests/test_illustrate_game_list_endpoint.py`（参照既有 `test_articles_ai_illustrate_endpoint.py` 的 `build_test_app` 用法 + MCP token header）：

```python
import pytest

pytestmark = pytest.mark.mysql


def test_ai_illustrate_with_game_positions_places_and_counts(monkeypatch, tmp_path):
    """显式 game_positions：命中 heading 的配图、未命中的进 missed，且不调 ai_format LLM。"""
    from server.app.tests.helpers import build_test_app  # 按仓库实际 helper 路径调整

    # 关键：断言确定性路径不调 LLM —— 把 _call_litellm_completion 打成抛错，若被调用则测试失败
    import server.app.modules.articles.ai_format as m
    monkeypatch.setattr(
        m, "_call_litellm_completion",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用 LLM")),
    )

    test_app = build_test_app(monkeypatch)
    try:
        client = test_app.client
        # 1) 建一篇含两个游戏 heading 的文章（main_category 已有图、第二个游戏无栏目）
        #    具体建文章/建栏目/上传图片复用既有 helper；此处省略，保持与 test_articles_ai_illustrate_endpoint 一致
        article_id = test_app.create_article_with_two_game_headings()  # helper
        main_cat_id = test_app.main_category_with_image()              # helper

        resp = client.post(
            f"/api/articles/{article_id}/ai-illustrate",
            headers={"X-MCP-Token": test_app.mcp_token},
            json={
                "main_category_id": main_cat_id,
                "include_companion": False,
                "web_fallback": False,
                "game_positions": [
                    {"game": "餐厅养成记"},      # 命中且有图
                    {"game": "查无此游戏"},       # 未命中 → missed
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["requested"] == 2
        assert data["images_inserted"] == 1
        assert data["missed"] == 1
        assert "查无此游戏" in data["missed_games"]
    finally:
        test_app.cleanup()
```

> 注：上面的 `create_article_with_two_game_headings` / `main_category_with_image` 是占位 helper 名——实现时按 `server/tests/test_articles_ai_illustrate_endpoint.py` 既有夹具方式内联建数据（建 StockCategory、上传一张图、建带两个 `## 游戏N、《名》` heading 的文章）。不要新造测试框架。

- [ ] **Step 2: 运行确认失败**

Run: `GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test pytest server/tests/test_illustrate_game_list_endpoint.py -q`
Expected: FAIL（端点尚不接受 `game_positions`，参数被忽略 → 走模型路径或 requested≠2）。

- [ ] **Step 3: 端点透传 `game_positions`**

`router.py` 的 `AiIllustratePayload` 加字段：

```python
    game_positions: list[dict] | None = None  # 上游显式游戏清单；None=走现有模型识别路径
```

`ai_illustrate_article_mcp` 里构造 `IllustrateOptions` 时加：

```python
            options=IllustrateOptions(
                include_companion=payload.include_companion,
                web_fallback=payload.web_fallback,
                aggressive_images=payload.aggressive_images,
                max_images=payload.max_images,
                min_spacing=payload.min_spacing,
                preset_id=payload.preset_id,
                set_cover=payload.set_cover,
                game_list=payload.game_positions,
            ),
```

- [ ] **Step 4: MCP 工具加参数**

`server/mcp/tools/action.py` 的 `ai_illustrate_article` 签名加 `game_positions: list[dict] | None = None`，并进 body：

```python
    body: dict[str, Any] = {
        "main_category_id": main_category_id,
        "include_companion": include_companion,
        "aggressive_images": aggressive_images,
        "set_cover": set_cover,
        "web_fallback": web_fallback,
    }
    if game_positions is not None:
        body["game_positions"] = game_positions
    return await _apost(f"/api/articles/{article_id}/ai-illustrate", json=body)
```

并在 docstring 补一句：`game_positions`：上游识别分支产出的 `[{"game": str, "category_id"?: int, "index"?: int}]`；给了则走确定性落图、按游戏名匹配 heading、不调配图模型。

- [ ] **Step 5: 运行确认通过**

Run: `GEO_TEST_DATABASE_URL=... pytest server/tests/test_illustrate_game_list_endpoint.py -q`
Expected: PASS（requested=2 / inserted=1 / missed=1 / 含未命中游戏；且 LLM 未被调用）。

- [ ] **Step 6: 回归 + lint + mypy**

Run: `ruff check server/ && ruff format --check server/ && mypy server/app`
Run: `GEO_TEST_DATABASE_URL=... pytest server/tests/test_articles_ai_illustrate_endpoint.py server/tests/test_ai_illustrate_svc.py -q`（缺省路径不回归）
Expected: 全绿。

- [ ] **Step 7: 提交**

```bash
git add server/app/modules/articles/router.py server/mcp/tools/action.py server/tests/test_illustrate_game_list_endpoint.py
git commit -m "feat(ai_illustrate): 端点+MCP 工具透传 game_positions 走确定性落图

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review（计划自查）

- **Spec 覆盖**：G1 落图=Task 2+3+4+5；G2 计数盲区=Task 3（expected=len 清单）+ 端点回传（Task 5 断言 missed）；G3 渲染=Task 1。接缝契约 `{game, index?, category_id?}`=Task 2/5。锁复用=Task 3 走 `_web_fallback_collect_and_write_back`（内置 `lock_started_at` 校验）。heading 不提升=Task 3 传 `set()`。✅ 全覆盖。
- **占位符扫描**：仅 Task 5 测试里 `create_article_with_two_game_headings` 等是**显式标注的夹具占位**，已说明照既有 endpoint 测试内联建数据——非代码逻辑占位。其余步骤均含可运行代码。
- **类型一致性**：`build_image_positions_from_game_list` 返回 `(list[dict], list[dict])` 在 Task 2 定义、Task 3 消费一致；`run_ai_format_from_game_list` 签名 Task 3 定义、Task 4 调用一致；`IllustrateOptions.game_list` Task 4 定义、Task 5 端点 `payload.game_positions` 透传一致。✅

## 待上游分支对齐（不阻塞本计划）

- `game_positions` 在 pipeline `ai_illustrate` 节点的 input mapping 字段名（本计划只接 MCP 端点；pipeline 节点接线等识别分支定型后追加一个小 Task）。
- 给了 `index` 与 heading 实算冲突时的优先级：当前实现 **heading 实算优先**，`index` 仅未命中时兜底（Task 2 已固化）。
