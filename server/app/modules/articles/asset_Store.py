from __future__ import annotations

import hashlib
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import exists, func, select, update as sa_update
from sqlalchemy.orm import Session

from server.app.core.paths import get_data_dir
from server.app.core.time import utcnow
from server.app.models import Article, ArticleBodyAsset, Asset, TaskLog
from server.app.shared.errors import ClientError


@dataclass(frozen=True)
class StoredAsset:
    asset: Asset
    path: Path


def guess_image_size(data: bytes) -> tuple[int | None, int | None]:
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


def normalize_ext(filename: str, content_type: str | None, data: bytes) -> str:
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
    data_dir = get_data_dir().resolve()
    path = (data_dir / asset.storage_key).resolve()
    if data_dir != path and data_dir not in path.parents:
        raise ClientError("Asset path escaped data directory")
    return path


def _create_asset(db: Session, user_id: int, data: bytes, filename: str, content_type: str) -> StoredAsset:
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
    return StoredAsset(asset=asset, path=path)


def store_bytes(db: Session, user_id: int, data: bytes, filename: str, content_type: str) -> StoredAsset:
    if not data:
        raise ValueError("Stored file is empty")
    return _create_asset(db, user_id, data, filename, content_type)


def _create_asset_from_path(
    db: Session, user_id: int, filepath: Path, filename: str, content_type: str,
    sha256_hash: str, size: int,
) -> StoredAsset:
    now = utcnow()
    asset_id = uuid.uuid4().hex
    with open(filepath, "rb") as f:
        header = f.read(32)
    ext = normalize_ext(filename, content_type, header)
    width, height = guess_image_size(header)
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
    return StoredAsset(asset=asset, path=dest)


async def store_upload(db: Session, user_id: int, upload: UploadFile) -> StoredAsset:
    import tempfile

    from fastapi import HTTPException

    from server.app.core.config import ALLOWED_MAGIC, MAX_ASSET_BYTES

    filename = upload.filename or f"{uuid.uuid4().hex}.bin"
    content_type = upload.content_type or "application/octet-stream"

    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp_path = Path(tmp.name)
    sha256 = hashlib.sha256()
    total = 0
    first_chunk = True

    try:
        while True:
            chunk = await upload.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ASSET_BYTES:
                tmp.close()
                tmp_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"File exceeds {MAX_ASSET_BYTES // (1024 * 1024)}MB limit",
                )

            if first_chunk:
                valid_magic = False
                for magic in ALLOWED_MAGIC:
                    if chunk.startswith(magic):
                        if magic == b"RIFF":
                            if len(chunk) >= 12 and chunk[8:12] == b"WEBP":
                                valid_magic = True
                                break
                        else:
                            valid_magic = True
                            break
                if not valid_magic:
                    tmp.close()
                    tmp_path.unlink(missing_ok=True)
                    raise HTTPException(status_code=415, detail="Unsupported file type")
                first_chunk = False

            tmp.write(chunk)
            sha256.update(chunk)

        tmp.close()

        if total == 0:
            tmp_path.unlink(missing_ok=True)
            raise ClientError("Uploaded file is empty")

        digest = sha256.hexdigest()
        existing = db.query(Asset).filter(Asset.sha256 == digest).first()
        if existing:
            existing_path = resolve_asset_path(existing)
            if existing_path.exists():
                tmp_path.unlink(missing_ok=True)
                db.flush()
                db.refresh(existing)
                return StoredAsset(asset=existing, path=existing_path)

        stored = _create_asset_from_path(db, user_id, tmp_path, filename, content_type, digest, total)
        db.flush()
        db.refresh(stored.asset)
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
    stmt = (
        select(Asset.id)
        .where(
            Asset.is_deleted == False,  # noqa: E712
            ~exists(select(Article.id).where(Article.cover_asset_id == Asset.id)),
            ~exists(select(ArticleBodyAsset.id).where(ArticleBodyAsset.asset_id == Asset.id)),
            ~exists(select(TaskLog.id).where(TaskLog.screenshot_asset_id == Asset.id)),
        )
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
    return result.rowcount


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

    cache_dir = get_data_dir() / "thumbnail_cache"
    cache_size = sum(f.stat().st_size for f in cache_dir.rglob("*") if f.is_file()) if cache_dir.exists() else 0

    return {
        "total_count": row.total_count,
        "total_size_bytes": row.total_size_bytes,
        "deleted_count": int(row.deleted_count or 0),
        "orphan_count": len(orphan_ids),
        "thumbnail_cache_size_bytes": cache_size,
    }
