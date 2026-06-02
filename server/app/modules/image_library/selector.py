from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session


@dataclass
class ImageQuery:
    # 旧字段保留，向后兼容（hook.py / ai_format.py 旧调用方不感知变化）
    category_id: int | None = None
    count: int = 1
    excluded_ids: list[int] = field(default_factory=list)
    article_context: str | None = None  # 预留：未来 AI 语义选图用

    # 新字段：支持多栏目 + hint 语义匹配
    category_ids: list[int] = field(default_factory=list)
    hint: str | None = None

    def __post_init__(self) -> None:
        # 兼容旧调用：category_id 有值但 category_ids 为空时，自动填充
        if self.category_id is not None and not self.category_ids:
            self.category_ids = [self.category_id]


@dataclass
class StockImageRef:
    id: int
    url: str  # /api/stock-images/{id}/file
    filename: str
    width: int | None
    height: int | None
    category_id: int | None = None
    official_url: str | None = None


# ══ THE UPGRADE POINT ════════════════════════════════════════════════════════
def pick_image_id(query: ImageQuery, db: Session) -> int | None:
    """从指定栏目随机取一张图的 ID，排除已选 ID。

    支持多栏目（category_ids）；兼容旧的单 category_id 调用。
    未来升级：改这一个函数实现 AI 语义选图 / AI 生图，调用方不感知变化。
    """
    from server.app.modules.image_library.models import StockImage

    if not query.category_ids:
        return None

    stmt = select(StockImage.id).where(StockImage.category_id.in_(query.category_ids))
    if query.excluded_ids:
        stmt = stmt.where(StockImage.id.notin_(query.excluded_ids))
    stmt = stmt.order_by(func.rand()).limit(1)
    return db.execute(stmt).scalar_one_or_none()


# ═════════════════════════════════════════════════════════════════════════════


def fetch_image_by_id(image_id: int, db: Session) -> StockImageRef | None:
    from server.app.modules.image_library.models import StockImage

    img = db.get(StockImage, image_id)
    if img is None:
        return None
    return StockImageRef(
        id=img.id,
        url=f"/api/stock-images/{img.id}/file",
        filename=img.filename,
        width=img.width,
        height=img.height,
        category_id=img.category_id,
        official_url=img.category.official_url if img.category is not None else None,
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


# ══ 语义匹配选图（新增）════════════════════════════════════════════════════════


def _fetch_candidates(
    category_ids: list[int],
    excluded_ids: set[int],
    db: Session,
) -> list:
    """批量获取指定栏目内（排除已用）的所有候选图片对象。"""
    from server.app.modules.image_library.models import StockImage

    stmt = select(StockImage).where(StockImage.category_id.in_(category_ids))
    if excluded_ids:
        stmt = stmt.where(StockImage.id.notin_(list(excluded_ids)))
    return list(db.execute(stmt).scalars().all())


def _match_by_hint(candidates: list, hint: str) -> int | None:
    """按优先级从候选图片中匹配 hint 关键词，返回匹配的 image_id 或 None。

    优先级：
    1. tags 中有元素包含 hint（大小写不敏感）
    2. description 包含 hint（大小写不敏感）
    3. 都不匹配 → None（不降级随机）
    """
    hint_lower = hint.lower()

    # 优先：标签匹配
    for img in candidates:
        tags: list[str] = img.tags or []
        if any(hint_lower in tag.lower() for tag in tags):
            return img.id

    # 次选：描述匹配
    for img in candidates:
        desc: str = img.description or ""
        if hint_lower in desc.lower():
            return img.id

    return None


def select_images_by_hints(
    category_ids: list[int],
    hints: list[str | None],
    db: Session,
) -> list[int | None]:
    """为每个图片位置按 hint 语义匹配一张图片。

    参数：
        category_ids: 可用图库分类 ID 列表
        hints: 每个图片位置的 hint 关键词（None 表示无 hint，直接跳过）
        db: SQLAlchemy session

    返回：
        list[int | None] — 每个位置对应的 stock_image_id（None 表示该位置不插图）

    规则：
        - hint 为 None/空 → 直接跳过，返回 None
        - hint 有值但无匹配 → 返回 None（不降级随机）
        - 已选图片不重复使用（候选池耗尽时放宽限制）
    """
    if not category_ids:
        return [None] * len(hints)

    used_ids: set[int] = set()
    result: list[int | None] = []

    for hint in hints:
        if not hint:
            result.append(None)
            continue

        # 先在排除已用 ID 的候选中匹配
        candidates = _fetch_candidates(category_ids, used_ids, db)
        matched_id = _match_by_hint(candidates, hint)

        # 候选池耗尽时放宽限制，允许复用图片
        if matched_id is None and used_ids:
            candidates_all = _fetch_candidates(category_ids, set(), db)
            matched_id = _match_by_hint(candidates_all, hint)

        if matched_id is not None:
            used_ids.add(matched_id)

        result.append(matched_id)

    return result
