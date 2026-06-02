"""AI 生文插图钩子 — 供 LangGraph 生文 fan-in 阶段调用。

调用方式：
    from server.app.modules.image_library.hook import insert_images_for_article
    insert_images_for_article(article_id, category_id, image_positions, db)

调用方不感知选图实现细节（随机 / AI 语义 / AI 生图均透明）。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

_logger = logging.getLogger(__name__)


def insert_images_for_article(
    article_id: int,
    category_id: int,
    image_positions: list[int],
    db: Session,
) -> None:
    """取图并插入文章正文指定位置。

    image_positions: 顶层 content 数组索引列表，表示"在该节点之后插图"。
    数量不足时按实际可用图数插入，不抛异常。
    """
    if not image_positions:
        return

    from server.app.core.time import utcnow
    from server.app.modules.articles.parser import dumps_content_json, loads_content_json
    from server.app.modules.articles.service import get_article
    from server.app.modules.image_library.inserter import insert_images_at_positions
    from server.app.modules.image_library.selector import ImageQuery, select_images

    article = get_article(db, article_id)
    if article is None or article.is_deleted:
        _logger.warning("insert_images_for_article: article %s not found", article_id)
        return

    refs = select_images(ImageQuery(category_id=category_id, count=len(image_positions)), db)
    if not refs:
        _logger.info("insert_images_for_article: no stock images in category %s", category_id)
        return

    content_json = loads_content_json(article.content_json)
    new_content_json = insert_images_at_positions(content_json, refs, image_positions)
    article.content_json = dumps_content_json(new_content_json)
    article.version += 1
    article.updated_at = utcnow()
    db.flush()
    _logger.info("inserted %d images into article %s", len(refs), article_id)
