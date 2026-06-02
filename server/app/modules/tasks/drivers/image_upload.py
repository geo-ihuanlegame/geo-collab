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
    """Yield a possibly-resized copy of image_path for Toutiao upload.

    If the image exceeds 1920 px wide or 2 MB, a downscaled JPEG temp file is
    yielded and cleaned up on exit.  Falls back to the original path silently
    on any PIL error so as not to block the publish flow.
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
