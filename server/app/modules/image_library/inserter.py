"""把选好的图片节点插进 Tiptap 文档（content_json）的纯函数集合。

只操作 content_json dict，不碰 DB；落库由调用方负责（按 CLAUDE.md 本应同步 content_html/plain_text，
但现有调用方 hook.insert_images_for_article 目前只回写 content_json + version）。
"""

from __future__ import annotations

from server.app.modules.image_library.selector import StockImageRef


def has_images_in_content(content_json: dict) -> bool:
    """检查 Tiptap doc 顶层节点是否已有 image 节点。"""
    for node in content_json.get("content") or []:
        if isinstance(node, dict) and node.get("type") == "image":
            return True
    return False


def build_image_node(ref: StockImageRef) -> dict:
    return {
        "type": "image",
        "attrs": {
            "src": ref.url,
            "alt": ref.filename,
            "title": "",
            "width": "100%",
            "stockImageId": ref.id,
        },
    }


def build_url_paragraph(url: str) -> dict:
    return {
        "type": "paragraph",
        "content": [{"type": "text", "text": url}],
    }


def insert_images_at_positions(
    content_json: dict,
    image_refs: list[StockImageRef],
    positions: list[int],
) -> dict:
    """在指定位置之后插入图片节点。

    positions 是顶层 content 数组的索引，表示"在该节点之后插入"。
    positions 和 image_refs 按顺序一一对应，多余的 refs 或 positions 被忽略。
    自动处理插入后的索引偏移。
    """
    if not image_refs or not positions:
        return content_json

    content = list(content_json.get("content") or [])
    pairs = list(zip(positions, image_refs, strict=False))
    # 按位置从大到小插入，避免正向插入时索引偏移问题
    pairs.sort(key=lambda p: p[0], reverse=True)

    for pos, ref in pairs:
        insert_at = min(pos + 1, len(content))
        nodes = [build_image_node(ref)]
        official_url = (ref.official_url or "").strip()
        if official_url:
            nodes.append(build_url_paragraph(official_url))
        content[insert_at:insert_at] = nodes

    return {**content_json, "content": content}
