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
        bq_parts: list[str] = []
        for child in content:
            _convert_block(child, image_urls, bq_parts)
        out.append(f"<blockquote>{''.join(bq_parts)}</blockquote>")
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
            inline_frag = _inline_html(content)
            if inline_frag:
                out.append(f"<p>{inline_frag}</p>")


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
