from __future__ import annotations

from pathlib import Path

import pytest

from server.app.modules.tasks.drivers.image_upload import _maybe_resize_for_upload

PIL_Image = pytest.importorskip("PIL.Image")


def _write_png(path: Path, width: int, height: int) -> Path:
    img = PIL_Image.new("RGB", (width, height), color=(123, 222, 64))
    img.save(path, "PNG")
    return path


def test_small_image_passthrough(tmp_path: Path) -> None:
    """低于大小和宽度限制的小图返回原路径，不生成临时文件。"""
    src = _write_png(tmp_path / "small.png", 10, 10)

    with _maybe_resize_for_upload(src) as out:
        assert out == src
        assert out.exists()

    # 原图不会被修改，且仍然存在。
    assert src.exists()


def test_wide_image_is_resized_to_jpeg_and_cleaned_up(tmp_path: Path) -> None:
    """宽度超过 1920px 的图片会生成临时 .jpg，退出上下文后删除。"""
    src = _write_png(tmp_path / "wide.png", 2000, 10)

    with _maybe_resize_for_upload(src) as out:
        assert out != src
        assert out.suffix == ".jpg"
        assert out.exists()
        resized_path = out

    # 上下文退出后临时文件会被清理，原图保留。
    assert not resized_path.exists()
    assert src.exists()
