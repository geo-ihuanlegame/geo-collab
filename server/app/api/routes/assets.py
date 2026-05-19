import os
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from server.app.core.paths import get_data_dir
from server.app.core.security import get_current_user, require_admin
from server.app.db.session import get_db
from server.app.models import Asset, User
from server.app.schemas.asset import AssetRead
from server.app.modules.articles import (
    asset_url,
    find_orphan_asset_ids,
    get_asset_stats,
    resolve_asset_path,
    soft_delete_assets,
    store_upload,
)
from server.app.shared.errors import ClientError

router = APIRouter()


def resolve_asset_path_from_storage_key(storage_key: str) -> Path | None:
    """根据 storage_key 解析磁盘路径，返回 None 如果路径逃逸"""
    try:
        data_dir = get_data_dir().resolve()
        path = (data_dir / storage_key).resolve()
        if data_dir != path and data_dir not in path.parents:
            return None
        return path
    except Exception:
        return None


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


@router.post("", response_model=AssetRead)
async def upload_asset(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    stored = await store_upload(db, current_user.id, file)
    return Response(
        content=to_asset_read(stored.asset).model_dump_json(),
        media_type="application/json",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/stats")
def asset_stats(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    """磁盘资产统计（总量、孤儿数、已删除数、缩略图缓存大小）。"""
    return get_asset_stats(db)


@router.post("/cleanup-orphans")
def cleanup_orphan_assets(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    """将所有孤儿资产（未被任何文章引用）标记为逻辑删除。不删除磁盘文件。"""
    orphan_ids = find_orphan_asset_ids(db)
    marked = soft_delete_assets(db, orphan_ids)
    return {"orphan_count": len(orphan_ids), "marked_deleted": marked}


@router.get("/{asset_id}/meta", response_model=AssetRead)
def read_asset_meta(asset_id: str, db: Session = Depends(get_db)) -> AssetRead:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return to_asset_read(asset)


@router.get("/{asset_id}/thumbnail")
async def read_asset_thumbnail(
    asset_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """获取资产缩略图，如果缩略图不存在则 302 重定向到原图"""
    asset = db.get(Asset, asset_id)
    if asset is None or asset.is_deleted:
        raise HTTPException(status_code=404)

    # 优先返回缩略图
    if asset.thumb_storage_key:
        thumb_path = resolve_asset_path_from_storage_key(asset.thumb_storage_key)
        if thumb_path and thumb_path.exists():
            if os.environ.get("GEO_NGINX_ACCEL"):
                rel = thumb_path.relative_to(get_data_dir())
                return Response(
                    status_code=200,
                    headers={
                        "X-Accel-Redirect": f"/internal_data/{rel}",
                        "Content-Type": "image/webp",
                        "Cache-Control": "public, max-age=31536000, immutable",
                    },
                )
            return FileResponse(str(thumb_path), media_type="image/webp")

    # fallback：缩略图不存在则 302 重定向到原图
    return RedirectResponse(url=f"/api/assets/{asset_id}", status_code=302)


@router.get("/{asset_id}")
def read_asset_file(
    asset_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    asset = db.get(Asset, asset_id)
    if asset is None or asset.is_deleted:
        raise HTTPException(status_code=404, detail="Asset not found")

    try:
        path = resolve_asset_path(asset)
    except (ClientError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not path.exists():
        raise HTTPException(status_code=404, detail="Asset file not found")

    # WebP content negotiation
    accept = request.headers.get("accept", "")
    mime_type = asset.mime_type
    if "image/webp" in accept and asset.webp_storage_key:
        webp_path = resolve_asset_path_from_storage_key(asset.webp_storage_key)
        if webp_path and webp_path.exists():
            path = webp_path
            mime_type = "image/webp"

    if os.environ.get("GEO_NGINX_ACCEL"):
        rel = path.relative_to(get_data_dir())
        filename_rfc5987 = quote(asset.filename.encode('utf-8'), safe='')
        return Response(
            status_code=200,
            headers={
                "X-Accel-Redirect": f"/internal_data/{rel}",
                "Content-Type": mime_type,
                "Content-Disposition": f"inline; filename*=UTF-8''{filename_rfc5987}",
                "Cache-Control": "public, max-age=31536000, immutable",
            },
        )

    filename_rfc5987 = quote(asset.filename.encode('utf-8'), safe='')
    return FileResponse(
        path,
        media_type=mime_type,
        filename=filename_rfc5987,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
