import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from PIL import Image
from sqlalchemy.orm import Session

from server.app.core.paths import get_data_dir
from server.app.core.security import get_current_user
from server.app.db.session import get_db
from server.app.models import Asset, User
from server.app.schemas.asset import AssetRead
from server.app.modules.articles import asset_url, resolve_asset_path, store_upload
from server.app.services.errors import ClientError

router = APIRouter()


def to_asset_read(asset: Asset) -> AssetRead:
    return AssetRead(
        id=asset.id,
        filename=asset.filename,
        ext=asset.ext,
        mime_type=asset.mime_type,
        size=asset.size,
        sha256=asset.sha256,
        storage_key=asset.storage_key,
        width=asset.width,
        height=asset.height,
        created_at=asset.created_at,
        url=asset_url(asset.id),
    )


def _generate_thumbnail(asset: Asset, width: int, data_dir: Path) -> Path | None:
    """Generate and cache a thumbnail for the given asset at the given width. Idempotent."""
    try:
        cache_dir = data_dir / "thumbnail_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{asset.id}_w{width}.jpg"

        if cache_file.exists():
            return cache_file

        asset_path = (data_dir / asset.storage_key).resolve()
        if not asset_path.exists():
            return None

        with Image.open(asset_path) as img:
            ratio = width / img.width
            new_height = int(img.height * ratio)
            img = img.resize((width, new_height), Image.LANCZOS)
            img = img.convert("RGB")
            img.save(cache_file, "JPEG", quality=85)
        return cache_file
    except Exception:
        return None


@router.post("", response_model=AssetRead)
async def upload_asset(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    stored = await store_upload(db, current_user.id, file)
    if stored.asset.mime_type.startswith("image/"):
        data_dir = get_data_dir()
        background_tasks.add_task(_generate_thumbnail, stored.asset, 600, data_dir)
        background_tasks.add_task(_generate_thumbnail, stored.asset, 300, data_dir)
    return Response(
        content=to_asset_read(stored.asset).model_dump_json(),
        media_type="application/json",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/{asset_id}/meta", response_model=AssetRead)
def read_asset_meta(asset_id: str, db: Session = Depends(get_db)) -> AssetRead:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return to_asset_read(asset)


@router.get("/{asset_id}")
async def read_asset_file(asset_id: str, width: int | None = None, db: Session = Depends(get_db)) -> Response:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    try:
        path = resolve_asset_path(asset)
    except (ClientError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not path.exists():
        raise HTTPException(status_code=404, detail="Asset file not found")

    if width is not None and asset.mime_type.startswith("image/"):
        cache_dir = get_data_dir() / "thumbnail_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{asset_id}_w{width}.jpg"

        if not cache_file.exists():
            await asyncio.to_thread(_generate_thumbnail, asset, width, get_data_dir())

        if not cache_file.exists():
            return FileResponse(
                path,
                media_type=asset.mime_type,
                filename=asset.filename,
                headers={"Cache-Control": "public, max-age=31536000, immutable"},
            )

        return FileResponse(
            cache_file,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    if os.environ.get("GEO_NGINX_ACCEL"):
        rel = path.relative_to(get_data_dir())
        return Response(
            status_code=200,
            headers={
                "X-Accel-Redirect": f"/internal_data/{rel}",
                "Content-Type": asset.mime_type,
                "Content-Disposition": f'inline; filename="{asset.filename}"',
            },
        )

    return FileResponse(
        path,
        media_type=asset.mime_type,
        filename=asset.filename,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
