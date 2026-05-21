"""Markdown → HTML / Tiptap JSON 转换工具。

Tiptap 支持的节点类型（本模块覆盖范围）：
  doc, paragraph, heading(level 1-6), bulletList, orderedList, listItem
  text marks: bold, italic, code
"""
from html.parser import HTMLParser
from typing import Any


def markdown_to_html(md: str) -> str:
    import markdown

    return markdown.markdown(md, extensions=["extra"])


class _TiptapBuilder(HTMLParser):
    """将 HTML 流式解析为 Tiptap JSON 节点树。"""

    def __init__(self) -> None:
        super().__init__()
        self._stack: list[dict[str, Any]] = []
        self._root: list[dict[str, Any]] = []
        self._marks: list[dict[str, Any]] = []

    # ── 内部辅助 ──────────────────────────────────────────────────────────

    def _current(self) -> dict[str, Any] | None:
        return self._stack[-1] if self._stack else None

    def _commit(self, node: dict[str, Any]) -> None:
        """把节点挂到父节点或根列表。"""
        if self._stack:
            self._stack[-1].setdefault("content", []).append(node)
        else:
            self._root.append(node)

    def _pop_commit(self) -> None:
        """弹出栈顶节点并挂到父级。"""
        node = self._stack.pop()
        self._commit(node)

    # ── HTMLParser 回调 ───────────────────────────────────────────────────

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._stack.append({"type": "heading", "attrs": {"level": int(tag[1])}, "content": []})
        elif tag == "p":
            self._stack.append({"type": "paragraph", "content": []})
        elif tag == "ul":
            self._stack.append({"type": "bulletList", "content": []})
        elif tag == "ol":
            self._stack.append({"type": "orderedList", "content": []})
        elif tag == "li":
            self._stack.append({"type": "listItem", "content": []})
        elif tag == "strong":
            self._marks.append({"type": "bold"})
        elif tag == "em":
            self._marks.append({"type": "italic"})
        elif tag in ("code", "tt"):
            self._marks.append({"type": "code"})

    def handle_endtag(self, tag: str) -> None:
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol"):
            if self._stack:
                self._pop_commit()
        elif tag == "li":
            if not self._stack:
                return
            item = self._stack.pop()
            # markdown 紧凑列表 <li>text</li> 不含 <p>，需手动包一层 paragraph
            has_block = any(
                c.get("type") in ("paragraph", "bulletList", "orderedList")
                for c in item.get("content", [])
            )
            if not has_block and item.get("content"):
                item["content"] = [{"type": "paragraph", "content": item["content"]}]
            self._commit(item)
        elif tag == "strong":
            self._marks = [m for m in self._marks if m["type"] != "bold"]
        elif tag == "em":
            self._marks = [m for m in self._marks if m["type"] != "italic"]
        elif tag in ("code", "tt"):
            self._marks = [m for m in self._marks if m["type"] != "code"]

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        node: dict[str, Any] = {"type": "text", "text": text}
        if self._marks:
            node["marks"] = list(self._marks)
        cur = self._current()
        if cur is not None:
            cur.setdefault("content", []).append(node)

    # ── 结果 ──────────────────────────────────────────────────────────────

    def result(self) -> dict[str, Any]:
        return {"type": "doc", "content": self._root}


def markdown_to_tiptap(md: str) -> dict[str, Any]:
    html = markdown_to_html(md)
    builder = _TiptapBuilder()
    builder.feed(html)
    return builder.result()
