"""
Asset（上传资源）存储层：落盘 / 取尺寸 / 派生 WebP+缩略图 / 路径解析 / 孤儿清理。

文件存到 data_dir/assets/YYYY/MM/<uuid><ext>，DB 里只记 storage_key（相对路径）。
按 sha256 去重；存图时尽力生成 WebP 全尺寸 + 400x400 缩略图供前端用。
resolve_asset_path 做逃逸校验（路径必须在 data_dir 内）。孤儿 = 未被任何文章封面/正文/任务截图引用的资产。
"""

from __future__ import annotations

import hashlib
import logging
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import exists, func, select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from server.app.core.paths import get_data_dir
from server.app.core.time import utcnow
from server.app.modules.articles.models import Article, ArticleBodyAsset, Asset
from server.app.shared.errors import ClientError

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredAsset:
    asset: Asset
    path: Path


def guess_image_size(data: bytes) -> tuple[int | None, int | None]:
    """从字节头解析图片宽高（PNG 读 IHDR，JPEG 扫 SOF marker）；非图/解析失败返回 (None, None)。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return width, height

    if data[:2] == b"\xff\xd8":
        index = 2
        while index < len(data):
            while index < len(data) and data[index] == 0xFF:
                index += 1
            if index >= len(data):
                break
            marker = data[index]
            index += 1
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(data):
                break
            segment_length = struct.unpack(">H", data[index : index + 2])[0]
            if marker in range(0xC0, 0xC4) and index + 7 <= len(data):
                height, width = struct.unpack(">HH", data[index + 3 : index + 7])
                return width, height
            index += segment_length

    return None, None


def _generate_derivatives(asset: Asset, src_path: Path) -> None:
    """生成 WebP 和缩略图派生文件，仅对图片类型执行"""
    if not (asset.mime_type or "").startswith("image/"):
        return
    try:
        from PIL import Image

        img = Image.open(src_path)
        # WebP 全尺寸
        webp_path = src_path.with_suffix(".webp")
        img.save(webp_path, "WEBP", quality=80, optimize=True)
        asset.webp_storage_key = Path(asset.storage_key).with_suffix(".webp").as_posix()
        asset.webp_size = webp_path.stat().st_size
        # 缩略图 400x400 WebP
        thumb = img.copy()
        thumb.thumbnail((400, 400))
        stem = Path(asset.storage_key).stem
        thumb_rel = Path(asset.storage_key).parent / f"{stem}_thumb.webp"
        thumb_path = src_path.parent / f"{src_path.stem}_thumb.webp"
        thumb.save(thumb_path, "WEBP", quality=75)
        asset.thumb_storage_key = thumb_rel.as_posix()
        asset.thumb_size = thumb_path.stat().st_size
    except Exception:
        _logger.warning(
            "Failed to generate image derivatives for asset %s", asset.id, exc_info=True
        )


def normalize_ext(filename: str, content_type: str | None, data: bytes) -> str:
    """推断文件扩展名：优先用文件名后缀，否则按魔数字节，再退而求其次用 content_type。"""
    suffix = Path(filename).suffix.lower()
    if suffix:
        return suffix

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"

    if data[:2] == b"\xff\xd8":
        return ".jpg"

    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"

    if content_type and "/" in content_type:
        return f".{content_type.split('/')[-1].lower()}"

    return ".bin"


def asset_url(asset_id: str) -> str:
    return f"/api/assets/{asset_id}"


def resolve_asset_path(asset: Asset) -> Path:
    """把 storage_key 解析成磁盘绝对路径，并校验未逃出 data_dir（防路径穿越）。"""
    data_dir = get_data_dir().resolve()
    path = (data_dir / asset.storage_key).resolve()
    if data_dir != path and data_dir not in path.parents:
        raise ClientError("Asset path escaped data directory")
    return path


def _create_asset(
    db: Session, user_id: int, data: bytes, filename: str, content_type: str
) -> StoredAsset:
    now = utcnow()
    asset_id = uuid.uuid4().hex
    ext = normalize_ext(filename, content_type, data)
    sha256 = hashlib.sha256(data).hexdigest()
    width, height = guess_image_size(data)
    storage_key = Path("assets") / f"{now:%Y}" / f"{now:%m}" / f"{asset_id}{ext}"

    asset = Asset(
        id=asset_id,
        user_id=user_id,
        filename=filename,
        ext=ext,
        mime_type=content_type,
        size=len(data),
        sha256=sha256,
        storage_key=storage_key.as_posix(),
        width=width,
        height=height,
    )
    db.add(asset)

    path = get_data_dir() / storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    db.flush()
    _generate_derivatives(asset, path)
    return StoredAsset(asset=asset, path=path)


def store_bytes(
    db: Session, user_id: int, data: bytes, filename: str, content_type: str
) -> StoredAsset:
    if not data:
        raise ValueError("Stored file is empty")
    return _create_asset(db, user_id, data, filename, content_type)


def _create_asset_from_path(
    db: Session,
    user_id: int,
    filepath: Path,
    filename: str,
    content_type: str,
    sha256_hash: str,
    size: int,
    ext: str,
    width: int | None,
    height: int | None,
    do_commit: bool = False,
) -> StoredAsset:
    now = utcnow()
    asset_id = uuid.uuid4().hex
    storage_key = Path("assets") / f"{now:%Y}" / f"{now:%m}" / f"{asset_id}{ext}"
    dest = get_data_dir() / storage_key

    asset = Asset(
        id=asset_id,
        user_id=user_id,
        filename=filename,
        ext=ext,
        mime_type=content_type,
        size=size,
        sha256=sha256_hash,
        storage_key=storage_key.as_posix(),
        width=width,
        height=height,
    )
    db.add(asset)
    db.flush()

    dest.parent.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.move(str(filepath), str(dest))
    _generate_derivatives(asset, dest)
    if do_commit:
        db.commit()
        db.refresh(asset)
    return StoredAsset(asset=asset, path=dest)


async def store_upload(db: Session, user_id: int, upload: UploadFile) -> StoredAsset:
    """小文件上传入口：边流式落临时文件边算 sha256/校验类型，按 sha256 去重命中则直接复用旧资产。

    超过 MAX_ASSET_BYTES 抛 413、首块魔数字节不在 ALLOWED_MAGIC 抛 415。
    实际建库+移动文件的同步活儿丢到 run_in_executor（DB session 非异步安全，由该线程独占完成）。
    出错时在 except 块清掉临时文件（成功路径下临时文件已被移动落库消耗）。
    """
    import aiofiles
    from fastapi import HTTPException

    from server.app.core.config import ALLOWED_MAGIC, MAX_ASSET_BYTES

    filename = upload.filename or f"{uuid.uuid4().hex}.bin"
    content_type = upload.content_type or "application/octet-stream"
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = data_dir / f".upload_{uuid.uuid4().hex}"

    sha256 = hashlib.sha256()
    total = 0
    first_chunk: bytes | None = None

    try:
        async with aiofiles.open(str(tmp_path), "wb") as tmp:
            while True:
                chunk = await upload.read(8388608)  # 8MB 分块
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ASSET_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds {MAX_ASSET_BYTES // (1024 * 1024)}MB limit",
                    )

                if first_chunk is None:
                    first_chunk = chunk
                    ok = any(chunk.startswith(m) for m in ALLOWED_MAGIC)
                    # RIFF 容器要再确认是 WebP（offset 8 处 "WEBP"），排除 wav/avi 等同样 RIFF 开头的非图
                    if (
                        ok
                        and chunk.startswith(b"RIFF")
                        and (len(chunk) < 12 or chunk[8:12] != b"WEBP")
                    ):
                        ok = False
                    if not ok:
                        raise HTTPException(status_code=415, detail="不支持的文件类型")

                await tmp.write(chunk)
                sha256.update(chunk)

        if total == 0:
            raise ClientError("Uploaded file is empty")

        # total > 0 保证循环至少跑过一次，因此 first_chunk 已设置。
        assert first_chunk is not None
        digest = sha256.hexdigest()
        # 去重：同 sha256 且磁盘文件还在 → 复用旧资产，不再落第二份
        existing = db.query(Asset).filter(Asset.sha256 == digest).first()
        if existing:
            existing_path = resolve_asset_path(existing)
            if existing_path.exists():
                return StoredAsset(asset=existing, path=existing_path)

        ext = normalize_ext(filename, content_type, first_chunk)
        width, height = guess_image_size(first_chunk)

        import asyncio

        loop = asyncio.get_event_loop()
        stored = await loop.run_in_executor(
            None,
            _create_asset_from_path,
            db,
            user_id,
            tmp_path,
            filename,
            content_type,
            digest,
            total,
            ext,
            width,
            height,
            True,
        )
        return stored

    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


# ── 孤儿资产管理 ──────────────────────────────────────────────────────────────


def find_orphan_asset_ids(db: Session) -> list[str]:
    """返回未被任何文章（封面/正文）或任务日志截图引用的 asset id 列表。"""
    from server.app.modules.tasks.models import TaskLog  # 懒导入，避免循环依赖

    stmt = select(Asset.id).where(
        Asset.is_deleted == False,  # noqa: E712
        ~exists(select(Article.id).where(Article.cover_asset_id == Asset.id)),
        ~exists(select(ArticleBodyAsset.id).where(ArticleBodyAsset.asset_id == Asset.id)),
        ~exists(select(TaskLog.id).where(TaskLog.screenshot_asset_id == Asset.id)),
    )
    return list(db.execute(stmt).scalars().all())


def soft_delete_assets(db: Session, asset_ids: list[str]) -> int:
    """将指定 asset 标记为逻辑删除，返回实际标记的数量。不删除磁盘文件。"""
    if not asset_ids:
        return 0
    now = utcnow()
    result = db.execute(
        sa_update(Asset)
        .where(Asset.id.in_(asset_ids), Asset.is_deleted == False)  # noqa: E712
        .values(is_deleted=True, deleted_at=now)
    )
    db.flush()
    return result.rowcount  # type: ignore[attr-defined]  # DML 执行返回 CursorResult


def get_asset_stats(db: Session) -> dict:
    """返回资产统计：总数、总大小、孤儿数、已删除数、缩略图缓存大小。"""
    row = db.execute(
        select(
            func.count().label("total_count"),
            func.coalesce(func.sum(Asset.size), 0).label("total_size_bytes"),
            func.sum(func.cast(Asset.is_deleted == True, Asset.size.type)).label("deleted_count"),  # noqa: E712
        )
    ).one()

    orphan_ids = find_orphan_asset_ids(db)

    return {
        "total_count": row.total_count,
        "total_size_bytes": row.total_size_bytes,
        "deleted_count": int(row.deleted_count or 0),
        "orphan_count": len(orphan_ids),
    }
