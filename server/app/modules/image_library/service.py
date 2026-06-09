"""图片库服务层：栏目/图片的可复用写操作，供路由之外的后台链路（如 AI配图联网兜底）调用。

路由里的 create_category / upload_image 带 audit、依赖 Request/User，后台线程用不了；
这里抽出无 HTTP 依赖的核心：get-or-create 栏目（中文名 + 拼音 bucket）、把字节落 MinIO + 建记录。
"""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.app.modules.image_library import store as minio_store
from server.app.modules.image_library.models import StockCategory, StockImage

logger = logging.getLogger(__name__)

_MIME_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


def slugify_bucket(name: str) -> str:
    """中文名 → 拼音 bucket 名，并规整到 S3 命名规范（小写字母数字，3~63 位）。

    例：「蛋仔派对」→ danzaipaidui。非中文字符按 pypinyin 原样保留再清洗；
    清洗后为空（极端情况）则回退随机串。不保证全局唯一，撞名由调用方加后缀。
    """
    from pypinyin import lazy_pinyin

    raw = "".join(lazy_pinyin(name or ""))
    slug = re.sub(r"[^a-z0-9]", "", raw.lower())
    if len(slug) < 3:
        slug = f"{slug}{uuid.uuid4().hex}"[:12]
    return slug[:63]


def _unique_bucket_name(db: Session, base: str) -> str:
    """在 base 基础上找一个未被占用的 bucket 名（撞名加数字后缀，保持 ≤63）。"""
    candidate = base
    n = 2
    while db.query(StockCategory).filter(StockCategory.bucket_name == candidate).first():
        suffix = str(n)
        candidate = f"{base[: 63 - len(suffix)]}{suffix}"
        n += 1
    return candidate


def get_or_create_companion_category(db: Session, name: str) -> StockCategory | None:
    """按中文名取陪衬栏目，没有就新建（拼音 bucket + 建 MinIO 桶）。并发撞名时回退取已存在的。

    name 为空返回 None。已存在同名栏目直接复用（不论 kind）。
    """
    name = (name or "").strip()
    if not name:
        return None

    existing = db.query(StockCategory).filter(StockCategory.name == name).first()
    if existing is not None:
        return existing

    bucket = _unique_bucket_name(db, slugify_bucket(name))
    try:
        minio_store.ensure_bucket(bucket)
    except Exception as exc:
        logger.warning("联网兜底建桶失败 name=%s bucket=%s：%s", name, bucket, exc)
        return None

    cat = StockCategory(name=name, bucket_name=bucket, kind="companion")
    db.add(cat)
    try:
        db.commit()
    except IntegrityError:
        # 并发下别的线程已建同名栏目：回退取它
        db.rollback()
        return db.query(StockCategory).filter(StockCategory.name == name).first()
    db.refresh(cat)
    logger.info("联网兜底新建陪衬栏目 name=%s bucket=%s id=%s", name, bucket, cat.id)
    return cat


def store_image_bytes(
    db: Session,
    category: StockCategory,
    data: bytes,
    content_type: str,
    *,
    source_url: str = "",
    width: int | None = None,
    height: int | None = None,
) -> StockImage | None:
    """把图片字节传 MinIO 并建 StockImage 记录。打 web_fallback 标签、description 存来源溯源。"""
    ext = _MIME_EXT.get(content_type, "jpg")
    key = f"{uuid.uuid4().hex}.{ext}"
    try:
        minio_store.upload_image(category.bucket_name, key, data, content_type)
    except Exception as exc:
        logger.warning("联网兜底上传 MinIO 失败 category=%s：%s", category.name, exc)
        return None

    img = StockImage(
        category_id=category.id,
        minio_key=key,
        filename=f"web_fallback_{key}",
        description=(f"联网兜底来源：{source_url}" if source_url else "联网兜底"),
        tags=["web_fallback"],
        width=width or None,
        height=height or None,
    )
    db.add(img)
    db.commit()
    db.refresh(img)
    logger.info("联网兜底入库图片 category=%s image_id=%s", category.name, img.id)
    return img
