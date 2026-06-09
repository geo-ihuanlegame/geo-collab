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
    """文章正文无法序列化为头条格式时抛出。"""


@dataclass(frozen=True)
class ImageRef:
    """以占位 token 输出的正文图片有序引用。

    实际上传和 token→`<img>` 替换稍后在 JS 中完成；序列化器只按文档顺序记录
    每个占位符对应的图片。
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
    """把解析后的正文片段序列化成头条 `<p data-track="N">` HTML。

    返回 ``(html, image_order)``，其中 ``image_order`` 按文档顺序列出每张正文图
    对应的 :class:`ImageRef`。每张图都会变成自己的占位段落
    ``<p data-track="N">__GEO_IMG_k__</p>``（k 从 0 开始）；真实上传和 token
    替换稍后在 JS 中完成。

    段落分隔符是 text 正好为 "\\n" 的文本片段。标题（h1/h2 都一样）渲染成
    头条的小标题节点 ``<h1 class="pgc-h-forward-slash">``，即 DOM 驱动通过
    编辑器 "# " input rule 生成的红点小标题；其它内容渲染为 ``<p>``。
    ``data-track`` 在所有段落中保持从 1 开始单调递增。
    """
    # 每项是 (inner_html, is_heading)；heading 标记决定最后包哪种标签，
    # data-track 在两类段落之间保持单调递增。
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
            # 小标题：heading 节点本身提供强调，不再额外加粗。
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
