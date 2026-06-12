"""微信图片压缩纯函数测试：Pillow 现场生成测试图，验证 64KB/1MB 边界与格式转换。"""

import io

import pytest
from PIL import Image

from server.app.modules.tasks.drivers.wechat_images import (
    CONTENT_IMAGE_MAX_BYTES,
    THUMB_MAX_BYTES,
    compress_content_image,
    compress_cover_to_jpeg,
)


def _image_bytes(mode: str, size: tuple[int, int], fmt: str) -> bytes:
    buf = io.BytesIO()
    img = Image.new(mode, size, color=(120, 30, 200) if mode == "RGB" else None)
    # 加噪声让 JPEG 不至于压得过小，测试更真实
    for x in range(0, size[0], 7):
        for y in range(0, size[1], 7):
            img.putpixel((x, y), (x % 256, y % 256, (x * y) % 256) if mode == "RGB" else x % 256)
    img.save(buf, format=fmt)
    return buf.getvalue()


def test_cover_small_jpeg_passthrough_still_jpeg():
    data = _image_bytes("RGB", (200, 150), "JPEG")
    out = compress_cover_to_jpeg(data)
    assert len(out) <= THUMB_MAX_BYTES
    assert Image.open(io.BytesIO(out)).format == "JPEG"


def test_cover_large_png_converted_and_compressed():
    data = _image_bytes("RGB", (2400, 1800), "PNG")
    assert len(data) > THUMB_MAX_BYTES
    out = compress_cover_to_jpeg(data)
    assert len(out) <= THUMB_MAX_BYTES
    assert Image.open(io.BytesIO(out)).format == "JPEG"


def test_cover_long_skinny_image_compressed_under_limit():
    data = _image_bytes("RGB", (20000, 64), "PNG")
    out = compress_cover_to_jpeg(data)
    assert len(out) <= THUMB_MAX_BYTES
    assert Image.open(io.BytesIO(out)).format == "JPEG"


def test_cover_rgba_png_flattened():
    buf = io.BytesIO()
    Image.new("RGBA", (800, 600), (255, 0, 0, 128)).save(buf, format="PNG")
    out = compress_cover_to_jpeg(buf.getvalue())
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG"
    assert img.mode == "RGB"


def test_content_image_small_png_kept_as_png():
    data = _image_bytes("RGB", (300, 200), "PNG")
    out, filename = compress_content_image(data, "x.png")
    assert out == data  # 已达标则原样返回
    assert filename.endswith(".png")


def test_content_image_small_gif_named_png_is_converted():
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), (10, 20, 30)).save(buf, format="GIF")
    data = buf.getvalue()
    out, filename = compress_content_image(data, "x.png")
    assert out != data
    assert filename.endswith(".jpg")
    assert Image.open(io.BytesIO(out)).format == "JPEG"


def test_content_image_invalid_named_png_is_rejected():
    with pytest.raises(ValueError, match="invalid image data"):
        compress_content_image(b"not an image", "x.png")


def test_content_image_oversize_recompressed_under_1mb():
    data = _image_bytes("RGB", (4000, 3000), "BMP")  # BMP 无压缩，必超 1MB
    assert len(data) > CONTENT_IMAGE_MAX_BYTES
    out, filename = compress_content_image(data, "x.bmp")
    assert len(out) <= CONTENT_IMAGE_MAX_BYTES
    assert filename.endswith(".jpg")
