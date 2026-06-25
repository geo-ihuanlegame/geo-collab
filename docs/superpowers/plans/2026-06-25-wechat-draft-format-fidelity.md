# 微信公众号草稿格式保真 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 `content_json → 保真 HTML` 纯函数转换器替换微信驱动里有损的 `BodySegment` 重建，让斜体 / 行内代码 / 链接 / 列表 / h3–h6 / 段内混排粗体在传到公众号草稿箱时不再丢失。

**Architecture:** 新增纯函数 `tiptap_to_wechat_html(content_json, image_urls)` 递归 Tiptap 文档树直接产出微信草稿 HTML；微信发布载荷从 `body_segments` 换成 `content_json` + `image_paths`（节点 key→本地路径）；驱动按节点 key 上传图片得到 `image_urls` 再喂转换器。生产已验证的 `wechat_client.py` / `wechat_images.py` 与发布 / 提交语义完全不动。

**Tech Stack:** Python 3.12、FastAPI、httpx（MockTransport 打桩）、Pillow、pytest。

## Global Constraints

- Python 命令前激活 conda 环境：`conda activate geo_xzpt`。
- 纯函数 / 驱动单测无需 DB；含 `@pytest.mark.mysql` 的用例需 `GEO_TEST_DATABASE_URL`（库名必须含 `test`），未设时自动跳过。
- 测试模块**不要在顶层 import** 任何会拉到 `server.app.db.session` 的模块（如 `runner_api` / `runner`）——`session.py` 加载即建引擎，collection 期无 DB 环境会 RuntimeError 拖垮整个 shard。需要时在测试函数内**惰性 import**。
- 后端硬门禁：`ruff check server/`、`ruff format --check server/`、`mypy server/app`、`pytest` 全绿。ruff line-length=100，选 E/F/I/B/UP，忽略 E501/B008。
- 驱动不碰 ORM（CLAUDE.md 铁律）：转换器与驱动只吃纯数据；ORM→payload 解析留在 `runner_api`。
- 提交语义零改动：图片上传走 `retry_call`（幂等）；`add_draft` 仍只进 `commit_guard.committing()`（非幂等、不盲重试）。
- 本任务无 DB / schema 改动、无迁移、无新配置项。

---

## File Structure

- `server/app/modules/articles/parser.py` — **修改**：新增通用 helper `image_node_key`（从 `taptap_contents.py` 迁入，与既有 `_asset_id_from_image_node` / `_stock_image_id_from_image_node` 同处）。
- `server/app/modules/tasks/drivers/taptap_contents.py` — **修改**：删除本地 `image_node_key` 定义，改从 `parser` 导入（行为不变）。
- `server/app/modules/tasks/drivers/wechat_html.py` — **新建**：`tiptap_to_wechat_html` 纯函数转换器。
- `server/app/modules/tasks/runner_api.py` — **修改**：抽共享 helper `_resolve_content_body`；`_build_api_payload`（微信）改产出 `content_json`+`image_paths`；`_build_cookie_payload`（TapTap）改用共享 helper。
- `server/app/modules/tasks/drivers/wechat_mp.py` — **修改**：`_publish_api` 改用 `content_json`+`image_paths`+新转换器；删除 `segments_to_html` 与对 `BodySegment` 的依赖。
- `server/tests/test_wechat_html.py` — **新建**：转换器单测。
- `server/tests/test_wechat_publish.py` — **改写**：驱动测试切到新载荷 + 断言保真 HTML；删除 `segments_to_html` 旧用例。
- `server/tests/test_runner_api_payload.py` — **新建**：`_resolve_content_body` 焦点单测（mysql 标记 + 惰性 import）。

执行顺序与依赖：Task 1 → Task 2（用到 Task 1 的 `image_node_key`）；Task 3 独立；Task 4 用到 Task 2 的转换器 + Task 3 的新载荷；Task 5 收尾验证。建议顺序 1→2→3→4→5。

---

### Task 1: 迁移 `image_node_key` 到 parser（行为保持）

**Files:**
- Modify: `server/app/modules/articles/parser.py`（在 `_stock_image_id_from_image_node` 之后新增函数，约第 75 行后）
- Modify: `server/app/modules/tasks/drivers/taptap_contents.py:16-35`（改导入、删本地定义）
- Test: `server/tests/test_taptap_contents.py`（既有，回归不破）

**Interfaces:**
- Produces: `server.app.modules.articles.parser.image_node_key(node: dict) -> str | None` —— 返回 asset_id 或 `"stock:<id>"`，无图返回 None。

- [ ] **Step 1: 在 `parser.py` 新增 `image_node_key`**

在 `server/app/modules/articles/parser.py` 的 `_stock_image_id_from_image_node` 函数定义之后插入：

```python
def image_node_key(node: dict[str, Any]) -> str | None:
    """图片节点的稳定 key：asset_id 或 ``stock:<id>``。

    驱动与各平台转换器共用，按 key 查找（而非顺序），重复用图 / 删图都稳。
    """
    asset_id = _asset_id_from_image_node(node)
    if asset_id:
        return asset_id
    stock_id = _stock_image_id_from_image_node(node)
    if stock_id is not None:
        return f"stock:{stock_id}"
    return None
```

- [ ] **Step 2: 改 `taptap_contents.py` 的导入并删除本地定义**

把 `server/app/modules/tasks/drivers/taptap_contents.py` 顶部的导入块（原 16-19 行）：

```python
from server.app.modules.articles.parser import (
    _asset_id_from_image_node,
    _stock_image_id_from_image_node,
)
```

改为：

```python
from server.app.modules.articles.parser import image_node_key
```

并**删除**本地的 `image_node_key` 定义（原 26-35 行整个函数，含其 docstring）。`_convert_block` 内对 `image_node_key(node)` 的调用保持不变。

- [ ] **Step 3: 跑回归测试确认不破**

Run: `pytest server/tests/test_taptap_contents.py -q`
Expected: PASS（全部既有用例通过，证明迁移行为等价）

- [ ] **Step 4: ruff 检查**

Run: `ruff check server/app/modules/articles/parser.py server/app/modules/tasks/drivers/taptap_contents.py`
Expected: 无 F401（未用导入）等报错

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/articles/parser.py server/app/modules/tasks/drivers/taptap_contents.py
git commit -m "refactor(parser): image_node_key 迁入通用 parser，两驱动平级解耦"
```

---

### Task 2: 新建保真转换器 `wechat_html.py`

**Files:**
- Create: `server/app/modules/tasks/drivers/wechat_html.py`
- Test: `server/tests/test_wechat_html.py`

**Interfaces:**
- Consumes: `parser.image_node_key`（Task 1）。
- Produces: `tiptap_to_wechat_html(content_json: dict | list, image_urls: dict[str, str] | None = None) -> str` —— 返回草稿 `content` 用 HTML 串；空文档 → `""`。

- [ ] **Step 1: 写失败测试 `test_wechat_html.py`**

创建 `server/tests/test_wechat_html.py`：

```python
"""tiptap_to_wechat_html 单测：纯函数，无 DB / 无网络。

覆盖：标题层级、行内混排不拆段、列表嵌套、引用 / 代码块、按 key 换图、
缺 url 跳过、未知节点降级、转义、空文档。
"""

from server.app.modules.tasks.drivers.wechat_html import tiptap_to_wechat_html


def _doc(*blocks):
    return {"type": "doc", "content": list(blocks)}


def _p(*inline):
    return {"type": "paragraph", "content": list(inline)}


def _text(t, *marks):
    node = {"type": "text", "text": t}
    if marks:
        node["marks"] = [{"type": m} for m in marks]
    return node


def test_headings_keep_all_levels():
    doc = _doc(
        {"type": "heading", "attrs": {"level": 1}, "content": [_text("一级")]},
        {"type": "heading", "attrs": {"level": 3}, "content": [_text("三级")]},
        {"type": "heading", "attrs": {"level": 6}, "content": [_text("六级")]},
    )
    html = tiptap_to_wechat_html(doc)
    assert "<h1>一级</h1>" in html
    assert "<h3>三级</h3>" in html
    assert "<h6>六级</h6>" in html


def test_inline_mixed_marks_stay_in_one_paragraph():
    # "普通 + 加粗 + 普通" 必须在同一个 <p> 内，不再被拆成多段
    doc = _doc(_p(_text("前"), _text("粗", "bold"), _text("后")))
    html = tiptap_to_wechat_html(doc)
    assert html == "<p>前<strong>粗</strong>后</p>"


def test_bold_italic_nesting():
    doc = _doc(_p(_text("x", "bold", "italic")))
    html = tiptap_to_wechat_html(doc)
    assert html == "<p><strong><em>x</em></strong></p>"


def test_inline_code_and_link():
    link_node = {
        "type": "text",
        "text": "点我",
        "marks": [{"type": "link", "attrs": {"href": "https://e.com/a?b=1"}}],
    }
    doc = _doc(_p(_text("代码", "code")), _p(link_node))
    html = tiptap_to_wechat_html(doc)
    assert "<code>代码</code>" in html
    assert '<a href="https://e.com/a?b=1">点我</a>' in html


def test_bullet_list_and_nested():
    doc = _doc(
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [_text("一")]},
                        {
                            "type": "bulletList",
                            "content": [
                                {
                                    "type": "listItem",
                                    "content": [{"type": "paragraph", "content": [_text("一一")]}],
                                }
                            ],
                        },
                    ],
                },
                {"type": "listItem", "content": [{"type": "paragraph", "content": [_text("二")]}]},
            ],
        }
    )
    html = tiptap_to_wechat_html(doc)
    assert html == "<ul><li>一<ul><li>一一</li></ul></li><li>二</li></ul>"


def test_ordered_list():
    doc = _doc(
        {
            "type": "orderedList",
            "content": [
                {"type": "listItem", "content": [{"type": "paragraph", "content": [_text("甲")]}]},
            ],
        }
    )
    assert tiptap_to_wechat_html(doc) == "<ol><li>甲</li></ol>"


def test_blockquote_and_codeblock():
    doc = _doc(
        {"type": "blockquote", "content": [{"type": "paragraph", "content": [_text("引用")]}]},
        {"type": "codeBlock", "content": [_text("a < b")]},
    )
    html = tiptap_to_wechat_html(doc)
    assert "<blockquote><p>引用</p></blockquote>" in html
    assert "<pre><code>a &lt; b</code></pre>" in html


def test_image_url_swapped_by_node_key():
    doc = _doc({"type": "image", "attrs": {"assetId": "a1"}})
    html = tiptap_to_wechat_html(doc, {"a1": "https://mmbiz.qpic.cn/1.jpg"})
    assert '<img src="https://mmbiz.qpic.cn/1.jpg" style="max-width:100%;">' in html


def test_image_without_url_is_skipped():
    doc = _doc({"type": "image", "attrs": {"assetId": "a1"}}, _p(_text("正文")))
    html = tiptap_to_wechat_html(doc, {})
    assert "<img" not in html
    assert "<p>正文</p>" in html


def test_unknown_block_degrades_to_paragraph():
    doc = _doc({"type": "weirdBlock", "content": [_text("怪块")]})
    assert tiptap_to_wechat_html(doc) == "<p>怪块</p>"


def test_text_escaped():
    doc = _doc(_p(_text("a<b>&\"c")))
    html = tiptap_to_wechat_html(doc)
    assert "a&lt;b&gt;&amp;" in html


def test_empty_paragraph_becomes_br():
    doc = _doc(_p())
    assert tiptap_to_wechat_html(doc) == "<p><br></p>"


def test_empty_doc_returns_empty_string():
    assert tiptap_to_wechat_html({"type": "doc", "content": []}) == ""
    assert tiptap_to_wechat_html({}) == ""
    assert tiptap_to_wechat_html([]) == ""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest server/tests/test_wechat_html.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'server.app.modules.tasks.drivers.wechat_html'`

- [ ] **Step 3: 实现 `wechat_html.py`**

创建 `server/app/modules/tasks/drivers/wechat_html.py`：

```python
"""Tiptap content_json → 微信公众号草稿保真 HTML（纯函数，零 I/O）。

设计稿 docs/superpowers/specs/2026-06-25-wechat-draft-format-fidelity-design.md。
图片 url 由驱动先传微信图床后以 image_urls（节点 key → url）喂进来；本模块不碰网络 / 磁盘。
未知 mark 丢标记留字、未知块降级 paragraph，绝不抛异常阻塞发布。
"""

from __future__ import annotations

import html as html_lib
from typing import Any

from server.app.modules.articles.parser import image_node_key

_HEADING_MAX = 6
_BLOCK_TYPES = frozenset(
    {"paragraph", "heading", "bulletList", "orderedList", "image", "blockquote", "codeBlock"}
)


def _inline_html(inline_nodes: list[Any] | None) -> str:
    """行内节点 → HTML 片段；marks 由内到外 code→em→strong→a 嵌套，文本与 href 转义。"""
    parts: list[str] = []
    for node in inline_nodes or []:
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        if node_type == "hardBreak":
            parts.append("<br>")
            continue
        if node_type != "text":
            continue
        text = node.get("text")
        if not isinstance(text, str) or text == "":
            continue
        frag = html_lib.escape(text)
        marks = node.get("marks") or []
        mark_types = {m.get("type") for m in marks if isinstance(m, dict)}
        if "code" in mark_types:
            frag = f"<code>{frag}</code>"
        if "italic" in mark_types:
            frag = f"<em>{frag}</em>"
        if "bold" in mark_types:
            frag = f"<strong>{frag}</strong>"
        if "link" in mark_types:
            link_mark = next(
                (m for m in marks if isinstance(m, dict) and m.get("type") == "link"), {}
            )
            href = (link_mark.get("attrs") or {}).get("href") or ""
            frag = f'<a href="{html_lib.escape(href)}">{frag}</a>'
        parts.append(frag)
    return "".join(parts)


def _list_html(node: dict[str, Any], image_urls: dict[str, str]) -> str:
    """bulletList / orderedList → <ul>/<ol>；listItem 内段落取行内、嵌套列表递归、其它块走块转换。"""
    tag = "ol" if node.get("type") == "orderedList" else "ul"
    items: list[str] = []
    for li in node.get("content") or []:
        if not isinstance(li, dict) or li.get("type") != "listItem":
            continue
        inner: list[str] = []
        for child in li.get("content") or []:
            if not isinstance(child, dict):
                continue
            ctype = child.get("type")
            if ctype in ("bulletList", "orderedList"):
                inner.append(_list_html(child, image_urls))
            elif ctype == "paragraph":
                inner.append(_inline_html(child.get("content")))
            else:
                _convert_block(child, image_urls, inner)
        items.append(f"<li>{''.join(inner)}</li>")
    return f"<{tag}>{''.join(items)}</{tag}>"


def _convert_block(node: Any, image_urls: dict[str, str], out: list[str]) -> None:
    if not isinstance(node, dict):
        return
    node_type = node.get("type")
    content = node.get("content") or []

    if node_type == "paragraph":
        inner = _inline_html(content)
        out.append(f"<p>{inner}</p>" if inner else "<p><br></p>")
    elif node_type == "heading":
        level = int((node.get("attrs") or {}).get("level", 1) or 1)
        level = min(max(level, 1), _HEADING_MAX)
        out.append(f"<h{level}>{_inline_html(content)}</h{level}>")
    elif node_type in ("bulletList", "orderedList"):
        out.append(_list_html(node, image_urls))
    elif node_type == "blockquote":
        inner: list[str] = []
        for child in content:
            _convert_block(child, image_urls, inner)
        out.append(f"<blockquote>{''.join(inner)}</blockquote>")
    elif node_type == "codeBlock":
        text = "".join(
            c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
        )
        out.append(f"<pre><code>{html_lib.escape(text)}</code></pre>")
    elif node_type == "image":
        key = image_node_key(node)
        url = image_urls.get(key) if key else None
        if url:
            out.append(f'<p><img src="{html_lib.escape(url)}" style="max-width:100%;"></p>')
    else:
        # 未知块：有块级子节点则递归，否则按段落输出其行内（优雅降级，不阻塞）
        if any(isinstance(c, dict) and c.get("type") in _BLOCK_TYPES for c in content):
            for child in content:
                _convert_block(child, image_urls, out)
        elif content:
            inner = _inline_html(content)
            if inner:
                out.append(f"<p>{inner}</p>")


def tiptap_to_wechat_html(
    content_json: dict[str, Any] | list[Any], image_urls: dict[str, str] | None = None
) -> str:
    """Tiptap 文档（doc dict 或裸 content 列表）→ 微信草稿 HTML 串。"""
    if isinstance(content_json, list):
        nodes = content_json
    elif isinstance(content_json, dict):
        nodes = content_json.get("content") or []
    else:
        nodes = []
    urls = image_urls or {}
    out: list[str] = []
    for node in nodes:
        _convert_block(node, urls, out)
    return "".join(out)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest server/tests/test_wechat_html.py -q`
Expected: PASS（全部用例）

- [ ] **Step 5: ruff / mypy**

Run: `ruff check server/app/modules/tasks/drivers/wechat_html.py && mypy server/app/modules/tasks/drivers/wechat_html.py`
Expected: 无报错

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/tasks/drivers/wechat_html.py server/tests/test_wechat_html.py
git commit -m "feat(wechat): content_json → 保真 HTML 转换器 + 单测"
```

---

### Task 3: `runner_api` 抽共享 helper + 微信载荷换 content_json

**Files:**
- Modify: `server/app/modules/tasks/runner_api.py`（新增 `_resolve_content_body`；重写 `_build_api_payload`；`_build_cookie_payload` 改用 helper）
- Test: `server/tests/test_runner_api_payload.py`（新建）

**Interfaces:**
- Produces: `_resolve_content_body(article) -> tuple[dict, dict[str, Path], list[Path]]` —— 返回 `(content_json, image_paths, temp_files)`；content_json 为空时用 plain_text 构造极简 doc；image_paths 按节点 key（asset_id / `stock:<id>`）映射本地路径，缺图（图库删图）跳过，解析失败清理 temp_files 后抛 `PublishError`。
- Produces: `_build_api_payload` 返回的 `ApiPublishPayload` 现填充 `content_json` + `image_paths`，`body_segments=[]`。

- [ ] **Step 1: 写失败测试 `test_runner_api_payload.py`**

创建 `server/tests/test_runner_api_payload.py`：

```python
"""_resolve_content_body 焦点单测：content_json 透传 + 空回落 plain_text。

标 mysql 是因为 import runner_api 会拉到 db.session（需引擎可构造）；import 放函数内（惰性）
以免 collection 期无 DB 拖垮 shard。无图用例不触 ORM 资产解析。
"""

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.mysql


def _fake_article(*, content_json: str, plain_text: str = ""):
    return SimpleNamespace(content_json=content_json, plain_text=plain_text, body_assets=[])


def test_content_json_passthrough_no_images():
    from server.app.modules.tasks.runner_api import _resolve_content_body

    raw = '{"type":"doc","content":[{"type":"paragraph","content":[{"type":"text","text":"正文"}]}]}'
    content_json, image_paths, temp_files = _resolve_content_body(_fake_article(content_json=raw))
    assert content_json["content"][0]["content"][0]["text"] == "正文"
    assert image_paths == {}
    assert temp_files == []


def test_empty_content_json_falls_back_to_plain_text():
    from server.app.modules.tasks.runner_api import _resolve_content_body

    content_json, image_paths, _ = _resolve_content_body(
        _fake_article(content_json="", plain_text="纯文本兜底")
    )
    assert content_json == {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "纯文本兜底"}]}],
    }
    assert image_paths == {}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `GEO_TEST_DATABASE_URL=$GEO_TEST_DATABASE_URL pytest server/tests/test_runner_api_payload.py -q`
Expected: FAIL —— `ImportError: cannot import name '_resolve_content_body'`（函数尚不存在）

- [ ] **Step 3: 在 `runner_api.py` 新增 `_resolve_content_body`**

在 `server/app/modules/tasks/runner_api.py` 顶部导入区补上（已有 `parse_body_segments` 等导入，确认含以下名字，缺则补）：

```python
from server.app.modules.articles.parser import (
    BodySegment,
    extract_body_image_nodes,
    extract_body_stock_image_nodes,
    loads_content_json,
    parse_body_segments,
)
```

在 `_resolve_access_token` 之后、`_build_api_payload` 之前插入：

```python
def _resolve_content_body(article: Article) -> tuple[dict, dict[str, Path], list[Path]]:
    """文章正文 → (content_json, image_paths, temp_files)。

    content_json 为空时用 plain_text 构造极简 doc。image_paths 按节点 key
    （asset_id / ``stock:<id>``）映射本地路径：正文图从 body_assets 解析、图库图取临时文件
    （已删的图库图跳过，照常发布，#36）。解析中途失败清理临时文件后抛 PublishError。
    """
    content_json = loads_content_json(article.content_json)
    if not content_json:
        body = (article.plain_text or "").strip()
        content_json = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": ([{"type": "text", "text": body}] if body else []),
                }
            ],
        }

    image_paths: dict[str, Path] = {}
    temp_files: list[Path] = []
    try:
        for asset_id, _node_id in extract_body_image_nodes(content_json):
            if asset_id in image_paths:
                continue
            asset_link = next(
                (
                    link
                    for link in article.body_assets
                    if link.asset_id == asset_id and link.asset is not None
                ),
                None,
            )
            if asset_link is None:
                raise PublishError(f"正文图片资源不存在或未加载: {asset_id}")
            image_paths[asset_id] = resolve_asset_path(asset_link.asset)

        stock_ids = extract_body_stock_image_nodes(content_json)
        if stock_ids:
            from server.app.modules.tasks.runner import _resolve_stock_image_path

            for stock_id in stock_ids:
                key = f"stock:{stock_id}"
                if key in image_paths:
                    continue
                image_path = _resolve_stock_image_path(stock_id, missing_ok=True)
                if image_path is None:
                    continue  # 图库图已删除：跳过该图，照常发布（#36）
                temp_files.append(image_path)
                image_paths[key] = image_path
        return content_json, image_paths, temp_files
    except Exception:
        if temp_files:
            from server.app.modules.tasks.runner import _cleanup_temp_files

            _cleanup_temp_files(temp_files)
        raise
```

- [ ] **Step 4: 跑测试确认通过**

Run: `GEO_TEST_DATABASE_URL=$GEO_TEST_DATABASE_URL pytest server/tests/test_runner_api_payload.py -q`
Expected: PASS（两条用例）

- [ ] **Step 5: 重写 `_build_api_payload`（微信）改用 helper**

把 `server/app/modules/tasks/runner_api.py` 的整个 `_build_api_payload` 函数体替换为：

```python
def _build_api_payload(
    article: Article, account: Account, access_token: str, platform_code: str
) -> ApiPublishPayload:
    """token 型平台（微信）载荷：content_json + image_paths（节点 key→本地路径）。

    封面可空——驱动内回落正文首图。platform_code 由调用方显式传入（避免懒加载已 detached 的
    account.platform，见 #90）。
    """
    cover_path: Path | None = None
    if article.cover_asset is not None:
        cover_path = resolve_asset_path(article.cover_asset)

    content_json, image_paths, temp_files = _resolve_content_body(article)
    return ApiPublishPayload(
        title=article.title,
        body_segments=[],
        cover_path=cover_path,
        display_name=account.display_name,
        platform_code=platform_code,
        access_token=access_token,
        content_json=content_json,
        image_paths=image_paths,
        temp_files=tuple(temp_files),
    )
```

- [ ] **Step 6: `_build_cookie_payload` 改用共享 helper**

在 `server/app/modules/tasks/runner_api.py` 的 `_build_cookie_payload` 中，把它内联的「content_json 回落 + image_paths / temp_files 解析」整段（从 `content_json = loads_content_json(...)` 到构造 `image_paths` / `temp_files` 的 for 循环、含外层 `try/except _cleanup_temp_files`）替换为一行调用，并直接用返回值构造 payload。改写后的函数主体如下（state / forum / x_ua 逻辑保持原样）：

```python
def _build_cookie_payload(
    article: Article, account: Account, platform_code: str
) -> ApiPublishPayload:
    """cookie-session 驱动（TapTap）payload：读解密 storage_state + 论坛配置 + content_json/图片。"""
    if not account.state_path:
        raise PublishError("TapTap 账号缺登录态（storage_state），请先在媒体矩阵登录")
    abs_state = get_data_dir() / account.state_path
    if not abs_state.exists():
        raise PublishError(f"TapTap 登录态文件不存在: {account.state_path}，请重新登录")
    state = read_state(abs_state)
    forum = dict(account.api_credentials or {})
    if not forum.get("x_ua") and account.platform_user_id:
        from server.app.modules.tasks.drivers.taptap_client import build_x_ua

        forum["x_ua"] = build_x_ua(account.platform_user_id)

    content_json, image_paths, temp_files = _resolve_content_body(article)
    return ApiPublishPayload(
        title=article.title,
        body_segments=[],
        cover_path=None,
        display_name=account.display_name,
        platform_code=platform_code,
        state=state,
        forum=forum,
        content_json=content_json,
        image_paths=image_paths,
        temp_files=tuple(temp_files),
    )
```

删除此函数中不再使用的局部导入与 `extract_body_image_nodes` 等内联逻辑。检查文件顶部：若 `loads_content_json` 等导入仅被 `_resolve_content_body` 使用，保持导入即可（它们现在被新 helper 使用）。

- [ ] **Step 7: 回归 TapTap 驱动 + ruff/mypy**

Run: `pytest server/tests/test_taptap_driver.py server/tests/test_taptap_contents.py server/tests/test_runner_api_payload.py -q`（后者需 `GEO_TEST_DATABASE_URL`）
Expected: PASS

Run: `ruff check server/app/modules/tasks/runner_api.py && mypy server/app/modules/tasks/runner_api.py`
Expected: 无报错（注意无 F401 未用导入）

- [ ] **Step 8: Commit**

```bash
git add server/app/modules/tasks/runner_api.py server/tests/test_runner_api_payload.py
git commit -m "refactor(runner_api): 抽 _resolve_content_body，微信载荷换 content_json+image_paths"
```

---

### Task 4: 微信驱动 `_publish_api` 切到保真转换器

**Files:**
- Modify: `server/app/modules/tasks/drivers/wechat_mp.py`（重写 `_publish_api`；删 `segments_to_html`；改导入）
- Test: `server/tests/test_wechat_publish.py`（改写）

**Interfaces:**
- Consumes: `tiptap_to_wechat_html`（Task 2）；`payload.content_json` + `payload.image_paths`（Task 3）。
- Produces: 草稿 `content` = 保真 HTML；`PublishResult.message` 含 `media_id`（不变）。

- [ ] **Step 1: 改写 `test_wechat_publish.py`**

将 `server/tests/test_wechat_publish.py` 全量替换为：

```python
"""微信驱动 publish_api 测试：MockTransport 全打桩，验证保真 HTML / 封面回落 / 错误映射。

驱动纯函数，无 DB。payload 直接给 content_json + image_paths（对齐 Task 3 后的载荷形态）。
"""

from pathlib import Path

import httpx
import pytest
from PIL import Image

from server.app.modules.tasks.drivers.base import ApiPublishPayload, PublishError
from server.app.modules.tasks.drivers.wechat_mp import WeChatMpDriver


def _jpeg_file(tmp_path: Path, name: str, size=(400, 300)) -> Path:
    p = tmp_path / name
    Image.new("RGB", size, (10, 20, 30)).save(p, format="JPEG")
    return p


def _payload(*, cover, content_json, image_paths):
    return ApiPublishPayload(
        title="测试标题",
        body_segments=[],
        cover_path=cover,
        display_name="测试公众号",
        platform_code="wechat_mp",
        access_token="tok",
        content_json=content_json,
        image_paths=image_paths,
    )


def _mock_client(uploads: list[str], captured: dict):
    """打桩：thumb→m1；uploadimg→递增 URL；draft/add→记录正文 body 后回 draft-1。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/cgi-bin/material/add_material":
            uploads.append("thumb")
            return httpx.Response(200, json={"media_id": "m1"})
        if path == "/cgi-bin/media/uploadimg":
            uploads.append("img")
            return httpx.Response(200, json={"url": f"https://mmbiz.qpic.cn/{len(uploads)}.jpg"})
        if path == "/cgi-bin/draft/add":
            uploads.append("draft")
            captured["body"] = request.content.decode("utf-8")
            return httpx.Response(200, json={"media_id": "draft-1"})
        raise AssertionError(f"unexpected path {path}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_publish_api_faithful_html_and_order(tmp_path):
    body_img = _jpeg_file(tmp_path, "body.jpg")
    cover = _jpeg_file(tmp_path, "cover.jpg")
    content_json = {
        "type": "doc",
        "content": [
            {"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": "小标题"}]},
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "条目"}]}],
                    }
                ],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "斜", "marks": [{"type": "italic"}]}],
            },
            {"type": "image", "attrs": {"assetId": "a1"}},
        ],
    }
    uploads: list[str] = []
    captured: dict = {}
    result = WeChatMpDriver().publish_api(
        payload=_payload(cover=cover, content_json=content_json, image_paths={"a1": body_img}),
        client=_mock_client(uploads, captured),
    )
    assert result.url is None
    assert "draft-1" in result.message
    assert uploads == ["thumb", "img", "draft"]
    # 草稿正文保住了被旧链路丢掉的格式
    assert "<h3>小标题</h3>" in captured["body"]
    assert "<ul><li>条目</li></ul>" in captured["body"]
    assert "<em>斜</em>" in captured["body"]
    assert "https://mmbiz.qpic.cn/2.jpg" in captured["body"]  # uploadimg 换好的图 url


def test_publish_api_cover_fallback_to_first_body_image(tmp_path):
    body_img = _jpeg_file(tmp_path, "body.jpg")
    content_json = {"type": "doc", "content": [{"type": "image", "attrs": {"assetId": "a1"}}]}
    uploads: list[str] = []
    captured: dict = {}
    result = WeChatMpDriver().publish_api(
        payload=_payload(cover=None, content_json=content_json, image_paths={"a1": body_img}),
        client=_mock_client(uploads, captured),
    )
    assert "draft-1" in result.message
    assert "thumb" in uploads  # 正文首图被用作封面上传


def test_publish_api_no_image_at_all_raises(tmp_path):
    content_json = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "只有文字"}]}],
    }
    with pytest.raises(PublishError, match="封面"):
        WeChatMpDriver().publish_api(
            payload=_payload(cover=None, content_json=content_json, image_paths={}),
            client=_mock_client([], {}),
        )


def test_publish_api_wechat_error_mapped_to_publish_error(tmp_path):
    cover = _jpeg_file(tmp_path, "cover.jpg")
    content_json = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "x"}]}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 45009, "errmsg": "api freq out of limit"})

    with pytest.raises(PublishError, match="45009"):
        WeChatMpDriver().publish_api(
            payload=_payload(cover=cover, content_json=content_json, image_paths={}),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest server/tests/test_wechat_publish.py -q`
Expected: FAIL（断言保真 HTML 的用例失败 / 或 `segments_to_html` 仍在被旧实现使用——尚未切换）

- [ ] **Step 3: 重写 `wechat_mp.py`**

改 `server/app/modules/tasks/drivers/wechat_mp.py`：

(a) 顶部导入：**删除** `from server.app.modules.articles.parser import BodySegment`，**新增** 转换器导入。改后顶部导入区为：

```python
from __future__ import annotations

import httpx

from server.app.modules.tasks.drivers import register
from server.app.modules.tasks.drivers.base import (
    NOOP_COMMIT_GUARD,
    ApiPublishPayload,
    PublishError,
    PublishResult,
)
from server.app.modules.tasks.drivers.wechat_client import (
    WeChatApiError,
    add_draft,
    build_draft_article,
    make_default_client,
    upload_content_image,
    upload_thumb,
)
from server.app.modules.tasks.drivers.wechat_html import tiptap_to_wechat_html
from server.app.modules.tasks.drivers.wechat_images import (
    compress_content_image,
    compress_cover_to_jpeg,
)
from server.app.shared.resilience import RetryPolicy, retry_call
```

(b) **删除** 整个 `segments_to_html` 函数定义（原 45-65 行，连同 `import html as html_lib`——确认 `html_lib` 在文件中已无其它使用后删除该 import）。

(c) 把 `_publish_api` 函数体替换为：

```python
    def _publish_api(
        self, *, payload: ApiPublishPayload, client: httpx.Client, commit_guard, policy
    ) -> PublishResult:
        token = payload.access_token
        image_paths = payload.image_paths or {}

        cover_path = payload.cover_path
        if cover_path is None:
            cover_path = next(iter(image_paths.values()), None)
        if cover_path is None:
            raise PublishError("公众号草稿需要封面图（或正文至少一张图）")

        def _do_thumb() -> str:
            return upload_thumb(
                token, "cover.jpg", compress_cover_to_jpeg(cover_path.read_bytes()), client=client
            )

        thumb_media_id = retry_call(_do_thumb, policy=policy, is_transient=_wechat_is_transient)

        image_urls: dict[str, str] = {}
        for key, path in image_paths.items():
            data, filename = compress_content_image(path.read_bytes(), path.name)

            def _do_content_image(_data: bytes = data, _filename: str = filename) -> str:
                return upload_content_image(token, _filename, _data, client=client)

            image_urls[key] = retry_call(
                _do_content_image, policy=policy, is_transient=_wechat_is_transient
            )

        content_html = tiptap_to_wechat_html(payload.content_json or {}, image_urls)
        if not content_html:
            raise PublishError("正文为空，无法创建公众号草稿")
        article = build_draft_article(
            title=payload.title, content_html=content_html, thumb_media_id=thumb_media_id
        )
        # 提交边界：add_draft 非幂等，不进 retry_call，只进 commit_guard
        with commit_guard.committing():
            media_id = add_draft(token, article, client=client)
        return PublishResult(
            url=None,
            title=payload.title,
            message=f"草稿已写入公众号草稿箱 media_id={media_id}",
        )
```

（`publish_api` 公共方法、`_wechat_is_transient`、类的其它方法与 `register(WeChatMpDriver())` 保持不变。）

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest server/tests/test_wechat_publish.py -q`
Expected: PASS（全部用例）

- [ ] **Step 5: ruff / mypy**

Run: `ruff check server/app/modules/tasks/drivers/wechat_mp.py && mypy server/app/modules/tasks/drivers/wechat_mp.py`
Expected: 无报错（确认无残留未用 import，如 `html`、`BodySegment`）

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/tasks/drivers/wechat_mp.py server/tests/test_wechat_publish.py
git commit -m "feat(wechat): 草稿正文改用保真 HTML 转换器，移除有损 segments_to_html"
```

---

### Task 5: 全量门禁 + 真实草稿人工验收

**Files:**
- 无代码改动（验证 + 文档勾选）

- [ ] **Step 1: 跑相关测试全绿**

Run: `pytest server/tests/test_wechat_html.py server/tests/test_wechat_publish.py server/tests/test_wechat_client.py server/tests/test_wechat_images.py server/tests/test_taptap_driver.py server/tests/test_taptap_contents.py -q`
Expected: PASS

- [ ] **Step 2: 后端硬门禁（与 CI 一致）**

Run: `ruff check server/ && ruff format --check server/ && mypy server/app`
Expected: 全部通过（如 `ruff format --check` 报需格式化，先 `ruff format server/` 再 commit）

- [ ] **Step 3: 真实草稿人工验收（核实微信 HTML 白名单，"核实而非复述"）**

对一个真实公众号账号（媒体矩阵已配 AppID/AppSecret），用一篇含 **h3 标题 / 有序列表 / 无序列表 / 斜体 / 行内代码 / 超链接 / 引用 / 正文图** 的文章触发一次发布（落草稿箱），然后登录 mp.weixin.qq.com 打开该草稿，逐项肉眼确认渲染：
- h3–h6 是否保留层级（若被吞 → 在 `wechat_html._convert_block` 的 heading 分支加降级：level≥3 输出 `<p><strong>…</strong></p>`）
- `<ul>/<ol>` 项目符号 / 编号是否在
- `<em>` / `<code>` / `<blockquote>` 是否渲染
- `<a>` 外链是否保留文字（未认证号去链接化属预期，文字不丢即可）
- 正文图是否为微信图床 URL（非外链）

把验收结果（截图或逐项 OK/降级）记录到本计划末尾或 PR 描述。**若需要降级**，回到 Task 2 改转换器对应分支 + 补单测，再重跑 Step 1–2。

- [ ] **Step 4: 收尾 commit（若 Step 3 触发了降级改动）**

```bash
git add server/app/modules/tasks/drivers/wechat_html.py server/tests/test_wechat_html.py
git commit -m "fix(wechat): 按真实草稿验收结果调整标签降级"
```

---

## Self-Review

**Spec coverage（逐节对照 2026-06-25 设计稿）：**
- §1 问题 / §3 转换器映射 → Task 2（含 h1–h6、行内不拆段、列表嵌套、blockquote、codeBlock、按 key 换图、降级、转义、空文档）。✅
- §4 解耦 `image_node_key` 归位 parser → Task 1。✅
- §5 载荷换 content_json + 共享 helper + 驱动 `_publish_api` 新流程 + 删 `segments_to_html` → Task 3（载荷 / helper）+ Task 4（驱动）。✅
- §6 微信侧 HTML 白名单核实 + 外链 → Task 5 Step 3（真实草稿验收 + 降级回路）。✅
- §7 测试三层 + 真实草稿验收 → Task 2 / Task 4 / Task 3 / Task 5。✅
- §8 无迁移 / 可回滚 → 计划全程无 schema 改动。✅

**Placeholder scan：** 无 TBD/TODO；每个代码步给出完整代码；每个测试步给出可运行断言。✅

**Type consistency：** `tiptap_to_wechat_html(content_json, image_urls=None) -> str`、`_resolve_content_body(article) -> (dict, dict[str,Path], list[Path])`、`image_node_key(node) -> str | None` 在定义任务与消费任务间签名一致；`ApiPublishPayload` 字段（`content_json` / `image_paths` / `body_segments` / `cover_path` / `access_token` / `temp_files`）沿用既有 dataclass 定义。✅
