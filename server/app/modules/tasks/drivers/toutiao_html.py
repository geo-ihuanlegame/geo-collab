"""把解析后的正文段落序列化成头条号正文 HTML（页内驱动专用）。

输出 `<p data-track="N">` / `<h1 class="pgc-h-forward-slash">`（头条小标题红点节点），
正文图片先落成占位 token 段落（__GEO_IMG_k__），真正的上传 + token→<img> 替换
延后到页内 JS 适配器里做。data-track 跨所有段落保持单调递增。
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path

from server.app.modules.articles.parser import BodySegment


class ToutiaoBodyError(Exception):
    """Raised when the article body cannot be serialized for Toutiao."""


@dataclass(frozen=True)
class ImageRef:
    """Ordered reference to a body image emitted as a placeholder token.

    The actual upload + token→`<img>` substitution happens later (in JS); the
    serializer only records what each placeholder stands for, in document order.
    """

    token: str
    image_path: Path | None
    image_asset_id: str | None = None
    stock_image_id: int | None = None


def _run_html(text: str, bold: bool) -> str:
    inner = escape(text, quote=False)
    return f"<strong>{inner}</strong>" if bold else inner


def body_segments_to_toutiao_html(
    segments: list[BodySegment],
) -> tuple[str, list[ImageRef]]:
    """Serialize parsed body segments into Toutiao `<p data-track="N">` HTML.

    Returns ``(html, image_order)`` where ``image_order`` lists, in document
    order, an :class:`ImageRef` per body image. Each image becomes its own
    placeholder paragraph ``<p data-track="N">__GEO_IMG_k__</p>`` (k = 0-based
    image index); the real upload + token substitution happens later in JS.

    Paragraph break = a text segment whose text is exactly "\\n".
    Headings (h1/h2 alike) render as Toutiao's 小标题 node
    ``<h1 class="pgc-h-forward-slash">`` — the red-dot subheading the DOM driver
    produces via the editor's "# " input rule; everything else as ``<p>``.
    ``data-track`` stays monotonic 1-based across ALL paragraphs.
    """
    # Each entry is (inner_html, is_heading); the heading flag selects the
    # wrapping tag at the end while data-track stays monotonic across both.
    paragraphs: list[tuple[str, bool]] = []
    image_order: list[ImageRef] = []
    current: list[str] = []
    current_is_heading = False

    def flush() -> None:
        nonlocal current_is_heading
        if current:
            joined = "".join(current)
            if joined.strip():
                paragraphs.append((joined, current_is_heading))
        current.clear()
        current_is_heading = False

    for seg in segments:
        if seg.kind == "image":
            flush()
            k = len(image_order)
            token = f"__GEO_IMG_{k}__"
            paragraphs.append((token, False))
            image_order.append(
                ImageRef(
                    token=token,
                    image_path=seg.image_path,
                    image_asset_id=seg.image_asset_id,
                    stock_image_id=seg.stock_image_id,
                )
            )
            continue
        if seg.text == "\n":
            flush()
            continue
        if not seg.text:
            continue
        if seg.heading_level is not None:
            # 小标题: the heading node supplies the emphasis — don't also bold it.
            current_is_heading = True
            current.append(_run_html(seg.text, False))
        else:
            current.append(_run_html(seg.text, seg.bold))
    flush()

    if not paragraphs:
        raise ToutiaoBodyError("正文为空")
    html_parts: list[str] = []
    for i, (inner, is_heading) in enumerate(paragraphs):
        track = i + 1
        if is_heading:
            html_parts.append(f'<h1 class="pgc-h-forward-slash" data-track="{track}">{inner}</h1>')
        else:
            html_parts.append(f'<p data-track="{track}">{inner}</p>')
    return "".join(html_parts), image_order
