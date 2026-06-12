"""微信平台图片规格压缩纯函数。

微信硬约束：封面 thumb 必须 JPG 且 ≤64KB；正文图 JPG/PNG 且 ≤1MB。
策略：先试原图/降质，不够再等比缩边，直到达标；全程纯函数，无 IO。
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

THUMB_MAX_BYTES = 64 * 1024
CONTENT_IMAGE_MAX_BYTES = 1024 * 1024

_QUALITY_LADDER = (85, 75, 65, 55, 45, 35)
_MIN_EDGE = 1  # shrink to a guaranteed fitting image before giving up
_PASSTHROUGH_FORMATS = {"JPEG", "PNG"}


def _flatten_to_rgb(img: Image.Image) -> Image.Image:
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return img.convert("RGB")


def _jpeg_bytes(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _compress_to_jpeg(data: bytes, max_bytes: int) -> bytes:
    """转 RGB JPEG 并迭代降质 + 等比缩边直到 ≤ max_bytes。"""
    img = _flatten_to_rgb(Image.open(io.BytesIO(data)))
    while True:
        for quality in _QUALITY_LADDER:
            out = _jpeg_bytes(img, quality)
            if len(out) <= max_bytes:
                return out
        width, height = img.size
        if min(width, height) <= _MIN_EDGE:
            return out
        img = img.resize((max(width // 2, _MIN_EDGE), max(height // 2, _MIN_EDGE)))


def _detect_image_format(data: bytes) -> str:
    try:
        with Image.open(io.BytesIO(data)) as img:
            image_format = img.format or ""
            img.verify()
            return image_format
    except OSError as exc:
        raise ValueError("invalid image data") from exc


def _content_filename(filename: str, image_format: str) -> str:
    path = Path(filename)
    stem = path.stem or "image"
    suffix = ".png" if image_format == "PNG" else ".jpg"
    return f"{stem}{suffix}"


def compress_cover_to_jpeg(data: bytes) -> bytes:
    """封面：任何输入格式 → RGB JPEG ≤64KB。"""
    return _compress_to_jpeg(data, THUMB_MAX_BYTES)


def compress_content_image(data: bytes, filename: str) -> tuple[bytes, str]:
    """正文图：已是 ≤1MB 的 JPG/PNG 原样返回；否则转 JPEG 压到 ≤1MB。

    返回 (bytes, 上传用文件名)。
    """
    image_format = _detect_image_format(data)
    if len(data) <= CONTENT_IMAGE_MAX_BYTES and image_format in _PASSTHROUGH_FORMATS:
        return data, _content_filename(filename, image_format)
    return _compress_to_jpeg(data, CONTENT_IMAGE_MAX_BYTES), "image.jpg"
