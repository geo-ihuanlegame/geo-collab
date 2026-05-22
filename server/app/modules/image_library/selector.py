from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session


@dataclass
class ImageQuery:
    category_id: int
    count: int
    excluded_ids: list[int] = field(default_factory=list)
    article_context: str | None = None  # 预留：未来 AI 语义选图用


@dataclass
class StockImageRef:
    id: int
    url: str        # /api/stock-images/{id}/file
    filename: str
    width: int | None
    height: int | None


# ══ THE UPGRADE POINT ════════════════════════════════════════════════════════
def pick_image_id(query: ImageQuery, db: Session) -> int | None:
    """从指定栏目随机取一张图的 ID，排除已选 ID。

    当前实现：ORDER BY RAND()。
    未来升级：改这一个函数实现 AI 语义选图 / AI 生图，调用方不感知变化。
    """
    from server.app.models.stock_image import StockImage

    stmt = select(StockImage.id).where(StockImage.category_id == query.category_id)
    if query.excluded_ids:
        stmt = stmt.where(StockImage.id.notin_(query.excluded_ids))
    stmt = stmt.order_by(func.rand()).limit(1)
    return db.execute(stmt).scalar_one_or_none()


# ═════════════════════════════════════════════════════════════════════════════

def fetch_image_by_id(image_id: int, db: Session) -> StockImageRef | None:
    from server.app.models.stock_image import StockImage

    img = db.get(StockImage, image_id)
    if img is None:
        return None
    return StockImageRef(
        id=img.id,
        url=f"/api/stock-images/{img.id}/file",
        filename=img.filename,
        width=img.width,
        height=img.height,
    )


def select_images(query: ImageQuery, db: Session) -> list[StockImageRef]:
    """取 query.count 张不重复的图，不足时按实际数量返回。"""
    excluded: list[int] = list(query.excluded_ids)
    results: list[StockImageRef] = []
    for _ in range(query.count):
        image_id = pick_image_id(dataclasses.replace(query, excluded_ids=excluded), db)
        if image_id is None:
            break
        excluded.append(image_id)
        ref = fetch_image_by_id(image_id, db)
        if ref:
            results.append(ref)
    return results
