"""上传前图片瘦身的共享工具。

被 DOM 驱动（toutiao.py）和页内驱动（toutiao_inpage.py）共用：超宽 / 超 2MB 的图
临时降采样为 JPEG 再上传，缩小上传体积、规避平台大小限制。出错时静默回退原图，
不阻断发布流程。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from server.app.shared.diagnostics import record_publish_diagnostic

logger = logging.getLogger(__name__)

_MAX_UPLOAD_WIDTH = 1920
_MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB


@contextmanager
def _maybe_resize_for_upload(image_path: Path) -> Iterator[Path]:
    """为头条上传产出可能已降采样的 image_path 副本。

    图片宽度超过 1920 px 或大小超过 2 MB 时，产出降采样后的 JPEG 临时文件，
    退出时清理。任何 PIL 错误都会静默回退原图，避免阻断发布流程。
    """
    tmp_path: Path | None = None
    try:
        try:
            from PIL import Image as _PILImage

            stat_size = image_path.stat().st_size
            with _PILImage.open(image_path) as _probe:
                orig_width, orig_height = _probe.width, _probe.height
            needs_resize = orig_width > _MAX_UPLOAD_WIDTH or stat_size > _MAX_UPLOAD_BYTES

            if needs_resize:
                import tempfile as _tempfile

                tmp = _tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                tmp_path = Path(tmp.name)
                tmp.close()
                with _PILImage.open(image_path) as _img:
                    out_img: _PILImage.Image = _img
                    if orig_width > _MAX_UPLOAD_WIDTH:
                        ratio = _MAX_UPLOAD_WIDTH / orig_width
                        out_img = _img.resize(
                            (_MAX_UPLOAD_WIDTH, int(orig_height * ratio)),
                            _PILImage.Resampling.LANCZOS,
                        )
                    out_img.convert("RGB").save(tmp_path, "JPEG", quality=85)
                record_publish_diagnostic(
                    f"image resized for upload: {image_path.name} "
                    f"({orig_width}px / {stat_size // 1024}KB) → JPEG 1920px"
                )
                yield tmp_path
                return
        except Exception:
            logger.warning("Image resize failed, uploading original: %s", image_path, exc_info=True)

        yield image_path
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
