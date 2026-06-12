"""从图库栏目随机取一张图、落成 Asset 并设为文章封面。

横跨 image_library（StockImage/MinIO）与 articles（Asset/cover_asset_id）两域的接缝单元：
ai_illustrate 节点在配完正文图后，可选地用「主推游戏」栏目的随机一张图补文章封面。

封面必须是 assets 表的 Asset（发布链路读本地文件路径），而图库里是 MinIO 上的 StockImage，
所以这里把选中的 StockImage 字节落成一个本地 Asset，再挂到 article.cover_asset_id。
选哪一张与正文插图无关（独立随机），不强求同一张。

全程 best-effort：任何一步失败都返回带 error 的 CoverResult，不抛异常、不影响发布状态。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

_logger = logging.getLogger(__name__)

# 扩展名 → content_type，仅用于标注 Asset.mime_type；真实 ext 由 store._create_asset
# 按字节魔数二次判定，故此处不必精确（兜底 image/jpeg）。
_EXT_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}


@dataclass
class CoverResult:
    status: str  # "set" | "skipped_existing" | "no_image" | "error"
    error: str | None = None


def _guess_content_type(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _EXT_MIME.get(ext, "image/jpeg")


def set_random_cover_from_category(
    db: Session,
    article: Any,
    category_id: int,
    user_id: int,
) -> CoverResult:
    """给 article 配封面：从 category_id 栏目随机取一张图落成 Asset 并设 cover_asset_id。

    仅当 article 还没封面时才设；已有封面保留不动。任何异常都吞掉并返回 CoverResult。
    只 flush，不 commit —— 由调用方提交（与本仓库后台链路一致）。
    """
    # 懒导入避免与 articles.store / selector 形成导入环
    from server.app.core.time import utcnow
    from server.app.modules.articles.store import store_bytes
    from server.app.modules.image_library import store as minio_store
    from server.app.modules.image_library.models import StockImage
    from server.app.modules.image_library.selector import ImageQuery, pick_image_id

    if getattr(article, "cover_asset_id", None):
        return CoverResult("skipped_existing")

    try:
        image_id = pick_image_id(ImageQuery(category_ids=[category_id]), db)
        if image_id is None:
            return CoverResult("no_image")

        image = db.get(StockImage, image_id)
        if image is None or image.category is None:
            return CoverResult("no_image")

        data = minio_store.get_object_bytes(image.category.bucket_name, image.minio_key)
        if not data:
            return CoverResult("no_image")

        content_type = _guess_content_type(image.minio_key or image.filename or "")
        stored = store_bytes(db, user_id, data, image.filename or "cover.jpg", content_type)
        article.cover_asset_id = stored.asset.id
        article.version += 1
        article.updated_at = utcnow()
        db.flush()
        _logger.info(
            "set cover for article %s from category %s (stock image %s)",
            getattr(article, "id", "?"),
            category_id,
            image_id,
        )
        return CoverResult("set")
    except Exception as exc:  # best-effort：封面失败不影响发布
        _logger.warning(
            "set_random_cover_from_category failed (article=%s category=%s): %s",
            getattr(article, "id", "?"),
            category_id,
            exc,
        )
        return CoverResult("error", str(exc))
