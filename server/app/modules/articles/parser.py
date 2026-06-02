from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BodySegment:
    kind: str  # "text" | "image"
    text: str = ""  # populated for kind="text"
    bold: bool = False  # text 节点有 bold mark
    heading_level: int | None = None  # 来自 heading 节点时为 1 或 2
    image_path: Path | None = None  # populated after resolution in publish_Runner
    image_asset_id: str | None = None  # populated by parser; used for tracing
    stock_image_id: int | None = None  # populated for image-library images


def _iter_nodes(node: Any) -> Iterable[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        content = node.get("content")
        if isinstance(content, list):
            for child in content:
                yield from _iter_nodes(child)
    elif isinstance(node, list):
        for child in node:
            yield from _iter_nodes(child)


def _asset_id_from_image_node(node: dict[str, Any]) -> str | None:
    attrs = node.get("attrs")
    if not isinstance(attrs, dict):
        return None
    for key in ("assetId", "asset_id", "dataAssetId"):
        value = attrs.get(key)
        if isinstance(value, str) and value:
            return value
    src = attrs.get("src")
    if isinstance(src, str) and "/api/assets/" in src:
        return src.rstrip("/").split("/api/assets/")[-1].split("?")[0]
    return None


def _stock_image_id_from_image_node(node: dict[str, Any]) -> int | None:
    attrs = node.get("attrs")
    if not isinstance(attrs, dict):
        return None
    for key in ("stockImageId", "stock_image_id", "dataStockImageId"):
        value = attrs.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    src = attrs.get("src")
    if isinstance(src, str):
        match = re.search(r"/api/stock-images/(\d+)/file", src)
        if match:
            return int(match.group(1))
    return None


def loads_content_json(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def dumps_content_json(content_json: dict[str, Any]) -> str:
    return json.dumps(content_json, ensure_ascii=False, separators=(",", ":"))


def extract_body_image_nodes(content_json: dict[str, Any]) -> list[tuple[str, str | None]]:
    """Return list of (asset_id, editor_node_id) for every image node in document order."""
    result = []
    for node in _iter_nodes(content_json):
        if node.get("type") != "image":
            continue
        asset_id = _asset_id_from_image_node(node)
        if not asset_id:
            continue
        raw_attrs = node.get("attrs")
        attrs = raw_attrs if isinstance(raw_attrs, dict) else {}
        editor_node_id = attrs.get("id") or attrs.get("nodeId")
        result.append((asset_id, editor_node_id))
    return result


def extract_body_stock_image_nodes(content_json: dict[str, Any]) -> list[int]:
    result = []
    for node in _iter_nodes(content_json):
        if node.get("type") != "image":
            continue
        stock_image_id = _stock_image_id_from_image_node(node)
        if stock_image_id is not None:
            result.append(stock_image_id)
    return result


def has_publishable_body(article: Any) -> bool:
    if (article.plain_text or "").strip():
        return True
    if re.sub(r"<[^>]+>", "", article.content_html or "").strip():
        return True
    content_json = loads_content_json(article.content_json)
    return bool(
        extract_body_image_nodes(content_json) or extract_body_stock_image_nodes(content_json)
    )


def _append_segments(
    node: Any, segments: list[BodySegment], depth: int = 0, _hlevel: int | None = None
) -> None:
    if isinstance(node, list):
        for child in node:
            _append_segments(child, segments, depth, _hlevel)
        return
    if not isinstance(node, dict):
        return

    node_type = node.get("type")

    if node_type == "text":
        text = node.get("text")
        if isinstance(text, str) and text:
            marks = node.get("marks") or []
            is_bold = any(isinstance(m, dict) and m.get("type") == "bold" for m in marks)
            segments.append(
                BodySegment(kind="text", text=text, bold=is_bold, heading_level=_hlevel)
            )
        return

    if node_type == "hardBreak":
        segments.append(BodySegment(kind="text", text="\n"))
        return

    if node_type == "image":
        asset_id = _asset_id_from_image_node(node)
        if asset_id:
            segments.append(BodySegment(kind="image", image_asset_id=asset_id))
            return
        stock_image_id = _stock_image_id_from_image_node(node)
        if stock_image_id is not None:
            segments.append(BodySegment(kind="image", stock_image_id=stock_image_id))
        return

    if node_type == "heading":
        level = int((node.get("attrs") or {}).get("level", 1))
        content = node.get("content")
        if isinstance(content, list):
            for child in content:
                _append_segments(child, segments, depth, _hlevel=level)
        segments.append(BodySegment(kind="text", text="\n"))
        return

    content = node.get("content")
    if isinstance(content, list):
        for child in content:
            _append_segments(
                child,
                segments,
                depth + (1 if node_type in ("orderedList", "bulletList") else 0),
                _hlevel=None,
            )

    if node_type == "paragraph":
        segments.append(BodySegment(kind="text", text="\n"))


def _compact(segments: list[BodySegment]) -> list[BodySegment]:
    compacted: list[BodySegment] = []
    for seg in segments:
        if seg.kind == "text":
            if not seg.text:
                continue
            if seg.text == "\n":
                compacted.append(seg)
                continue
            if (
                compacted
                and compacted[-1].kind == "text"
                and compacted[-1].text != "\n"
                and compacted[-1].bold == seg.bold
                and compacted[-1].heading_level == seg.heading_level
            ):
                prev = compacted.pop()
                compacted.append(
                    BodySegment(
                        kind="text",
                        text=prev.text + seg.text,
                        bold=prev.bold,
                        heading_level=prev.heading_level,
                    )
                )
            else:
                compacted.append(seg)
        else:
            compacted.append(seg)
    while compacted and compacted[-1].kind == "text" and not compacted[-1].text.strip():
        compacted.pop()
    return compacted


def parse_body_segments(article: Any) -> list[BodySegment]:
    """Parse article body into ordered text/image segments.

    Image segments have image_asset_id set and image_path=None.
    publish_Runner resolves image_path before passing to drivers.
    """
    content_json = loads_content_json(article.content_json)
    segments: list[BodySegment] = []
    _append_segments(content_json, segments)
    segments = _compact(segments)
    if segments:
        return segments
    body = (article.plain_text or re.sub(r"<[^>]+>", "", article.content_html or "")).strip()
    return [BodySegment(kind="text", text=body)] if body else []
