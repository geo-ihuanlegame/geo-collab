"""Tiptap content_json → TapTap 长帖 contents 转换器（纯函数，零 I/O）。

设计稿 docs/plans/2026-06-23-taptap-driver.md §5。图片 url 由驱动先传七牛拿到后以
`image_urls`（节点 key → url）喂进来；本模块不碰网络/磁盘，便于单测。

TapTap contents = Slate 风格 block：
  paragraph / heading(info.level 1|2) / list(info.style numbered|default + list-item info.li-level)
  / image(info.img_url)；行内叶子 {text} / {text,bold} / {type:link,children,info.url}。
未知 mark（斜体等）丢标记留字、未知块降级 paragraph，绝不阻塞（契约见 spike CONTRACT.md）。
"""

from __future__ import annotations

from typing import Any

from server.app.modules.articles.parser import image_node_key

_BLOCK_TYPES = frozenset(
    {"paragraph", "heading", "bulletList", "orderedList", "image", "blockquote", "codeBlock"}
)


def _leaf_from_text(node: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """text 节点 → (叶子, 是否 link)。bold 落叶子属性；link 包成行内元素（bold 留在 children 内）。"""
    text = node.get("text") or ""
    marks = node.get("marks") or []
    mark_types = {m.get("type") for m in marks if isinstance(m, dict)}
    leaf: dict[str, Any] = {"text": text}
    if "bold" in mark_types:
        leaf["bold"] = True
    if "link" in mark_types:
        link_mark = next((m for m in marks if isinstance(m, dict) and m.get("type") == "link"), {})
        href = (link_mark.get("attrs") or {}).get("href") or ""
        return {"type": "link", "children": [leaf], "info": {"url": href}}, True
    return leaf, False


def _leaves(inline_nodes: list[Any] | None) -> list[dict[str, Any]]:
    """行内节点 → TapTap 叶子列表；link 前后按 Slate 规则垫空 {text:""}（若未被普通文本夹住）。"""
    raw: list[tuple[dict[str, Any], bool]] = []
    for node in inline_nodes or []:
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        if node_type == "text":
            raw.append(_leaf_from_text(node))
        elif node_type == "hardBreak":
            raw.append(({"text": "\n"}, False))
        # 其它行内类型忽略

    result: list[dict[str, Any]] = []
    for i, (leaf, is_link) in enumerate(raw):
        if is_link:
            if not result or "type" in result[-1]:  # 前面不是普通文本叶子 → 垫空
                result.append({"text": ""})
            result.append(leaf)
            nxt = raw[i + 1] if i + 1 < len(raw) else None
            if nxt is None or nxt[1]:  # 后面没有或又是 link → 垫空
                result.append({"text": ""})
        else:
            result.append(leaf)
    return result or [{"text": ""}]


def _collect_list_items(
    list_node: dict[str, Any], li_level: int, items: list[dict[str, Any]]
) -> None:
    """把列表节点拍平成 list-item 列表；嵌套列表递归 li_level+1（flatten 进同一父 list.children）。"""
    for li in list_node.get("content") or []:
        if not isinstance(li, dict) or li.get("type") != "listItem":
            continue
        leaves: list[dict[str, Any]] = []
        nested: list[dict[str, Any]] = []
        for child in li.get("content") or []:
            if not isinstance(child, dict):
                continue
            if child.get("type") in ("bulletList", "orderedList"):
                nested.append(child)
            else:  # paragraph 或其它：取其行内
                leaves.extend(_leaves(child.get("content") or []))
        items.append(
            {
                "type": "list-item",
                "children": leaves or [{"text": ""}],
                "info": {"li-level": li_level},
            }
        )
        for nested_list in nested:
            _collect_list_items(nested_list, li_level + 1, items)


def _convert_block(node: Any, image_urls: dict[str, str], out: list[dict[str, Any]]) -> None:
    if not isinstance(node, dict):
        return
    node_type = node.get("type")
    content = node.get("content") or []

    if node_type == "paragraph":
        out.append({"type": "paragraph", "children": _leaves(content)})
    elif node_type == "heading":
        level = int((node.get("attrs") or {}).get("level", 1) or 1)
        out.append(
            {
                "type": "heading",
                "children": _leaves(content),
                "info": {"level": min(max(level, 1), 2)},
            }
        )
    elif node_type in ("bulletList", "orderedList"):
        items: list[dict[str, Any]] = []
        _collect_list_items(node, 1, items)
        style = "numbered" if node_type == "orderedList" else "default"
        out.append({"type": "list", "info": {"style": style}, "children": items})
    elif node_type == "image":
        key = image_node_key(node)
        url = image_urls.get(key) if key else None
        if url:
            out.append({"type": "image", "info": {"img_url": url, "description": ""}})
    elif node_type == "codeBlock":
        text = "".join(
            c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
        )
        out.append({"type": "paragraph", "children": [{"text": text}] if text else [{"text": ""}]})
    else:
        # 未知块（blockquote 等）：有块级子节点则递归，否则按段落处理其行内
        if any(isinstance(c, dict) and c.get("type") in _BLOCK_TYPES for c in content):
            for child in content:
                _convert_block(child, image_urls, out)
        elif content:
            out.append({"type": "paragraph", "children": _leaves(content)})


def tiptap_to_contents(
    content_json: dict[str, Any] | list[Any], image_urls: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Tiptap 文档（doc dict 或裸 content 列表）→ TapTap contents 块列表。

    image_urls: 节点 key（image_node_key）→ 七牛 url；缺 key 的图片块跳过。
    """
    if isinstance(content_json, list):
        nodes = content_json
    elif isinstance(content_json, dict):
        nodes = content_json.get("content") or []
    else:
        nodes = []
    out: list[dict[str, Any]] = []
    urls = image_urls or {}
    for node in nodes:
        _convert_block(node, urls, out)
    return out
