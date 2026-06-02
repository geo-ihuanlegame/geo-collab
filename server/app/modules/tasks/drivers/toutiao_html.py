from __future__ import annotations

from html import escape

from server.app.modules.articles.parser import BodySegment


class ToutiaoBodyError(Exception):
    """Raised when the article body cannot be serialized for Toutiao."""


def _run_html(text: str, bold: bool) -> str:
    inner = escape(text, quote=False)
    return f"<strong>{inner}</strong>" if bold else inner


def body_segments_to_toutiao_html(segments: list[BodySegment]) -> str:
    """Serialize parsed body segments into Toutiao `<p data-track="N">` HTML.

    Paragraph break = a text segment whose text is exactly "\\n".
    Headings render as a bold paragraph (M1 has no dedicated heading tag).
    Image segments are rejected in M1 (require the upload API — Milestone 2).
    """
    paragraphs: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            joined = "".join(current)
            if joined.strip():
                paragraphs.append(joined)
        current.clear()

    for seg in segments:
        if seg.kind == "image":
            raise ToutiaoBodyError(
                f"正文图片暂不支持（Milestone 2）: asset_id={seg.image_asset_id}"
            )
        if seg.text == "\n":
            flush()
            continue
        if not seg.text:
            continue
        bold = seg.bold or seg.heading_level is not None
        current.append(_run_html(seg.text, bold))
    flush()

    if not paragraphs:
        raise ToutiaoBodyError("正文为空")
    return "".join(f'<p data-track="{i + 1}">{p}</p>' for i, p in enumerate(paragraphs))
