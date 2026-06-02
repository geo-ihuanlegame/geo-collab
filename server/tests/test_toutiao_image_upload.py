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
    """A small image under the size/width caps yields the same path, no temp file."""
    src = _write_png(tmp_path / "small.png", 10, 10)

    with _maybe_resize_for_upload(src) as out:
        assert out == src
        assert out.exists()

    # Original is untouched and still present.
    assert src.exists()


def test_wide_image_is_resized_to_jpeg_and_cleaned_up(tmp_path: Path) -> None:
    """A >1920px-wide image yields a different .jpg temp that is removed on exit."""
    src = _write_png(tmp_path / "wide.png", 2000, 10)

    with _maybe_resize_for_upload(src) as out:
        assert out != src
        assert out.suffix == ".jpg"
        assert out.exists()
        resized_path = out

    # Temp file is cleaned up after the context exits; original remains.
    assert not resized_path.exists()
    assert src.exists()
