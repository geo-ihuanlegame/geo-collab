"""图片库模块路由。"""
from __future__ import annotations

import struct
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from server.app.core.security import get_current_user
from server.app.db.session import get_db
from server.app.modules.audit.service import add_audit_entry
from server.app.modules.image_library.models import StockCategory, StockImage
from server.app.modules.system.models import User
from server.app.modules.image_library import store as minio_store

router = APIRouter()          # /api/image-library/* — 需要登录
files_router = APIRouter()   # /api/stock-images/*  — 公开（图片嵌入文章）


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    bucket_name: str = Field(min_length=1, max_length=63)
    description: str | None = None
    official_url: str | None = None

    @field_validator("official_url", mode="before")
    @classmethod
    def normalize_official_url(cls, value: Any) -> str | None:
        return _normalize_official_url(value)


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    official_url: str | None = None

    @field_validator("official_url", mode="before")
    @classmethod
    def normalize_official_url(cls, value: Any) -> str | None:
        return _normalize_official_url(value)


class CategoryRead(BaseModel):
    id: int
    name: str
    bucket_name: str
    description: str | None
    official_url: str | None
    created_at: datetime


class StockImageRead(BaseModel):
    id: int
    category_id: int
    minio_key: str
    filename: str
    description: str | None
    tags: list[str]
    width: int | None
    height: int | None
    url: str
    created_at: datetime


# ── Helpers ───────────────────────────────────────────────────────────────────

def _guess_image_size(data: bytes) -> tuple[int | None, int | None]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    if data[:2] == b"\xff\xd8":
        idx = 2
        while idx < len(data):
            while idx < len(data) and data[idx] == 0xFF:
                idx += 1
            if idx >= len(data):
                break
            marker = data[idx]
            idx += 1
            if marker in {0xD8, 0xD9}:
                continue
            if idx + 2 > len(data):
                break
            seg_len = struct.unpack(">H", data[idx: idx + 2])[0]
            if marker in range(0xC0, 0xC4) and idx + 7 <= len(data):
                h, w = struct.unpack(">HH", data[idx + 3: idx + 7])
                return w, h
            idx += seg_len
    return None, None


def _normalize_official_url(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("official_url must be a string")
    trimmed = value.strip()
    if not trimmed:
        return None
    parsed = urlparse(trimmed)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("official_url must start with http:// or https://")
    return trimmed


def _to_category_read(cat: StockCategory) -> CategoryRead:
    return CategoryRead(
        id=cat.id,
        name=cat.name,
        bucket_name=cat.bucket_name,
        description=cat.description,
        official_url=cat.official_url,
        created_at=cat.created_at,
    )


def _to_image_read(img: StockImage) -> StockImageRead:
    return StockImageRead(
        id=img.id,
        category_id=img.category_id,
        minio_key=img.minio_key,
        filename=img.filename,
        description=img.description,
        tags=img.tags or [],
        width=img.width,
        height=img.height,
        url=f"/api/stock-images/{img.id}/file",
        created_at=img.created_at,
    )


# ── Category routes ───────────────────────────────────────────────────────────

@router.post("/categories", response_model=CategoryRead, status_code=201)
def create_category(
    payload: CategoryCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    existing = db.query(StockCategory).filter(StockCategory.bucket_name == payload.bucket_name).first()
    if existing:
        raise HTTPException(status_code=409, detail="bucket_name 已存在")
    try:
        minio_store.ensure_bucket(payload.bucket_name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"MinIO bucket 创建失败: {exc}") from exc
    cat = StockCategory(
        name=payload.name,
        bucket_name=payload.bucket_name,
        description=payload.description,
        official_url=payload.official_url,
    )
    db.add(cat)
    db.commit()
    db.refresh(cat)
    add_audit_entry(
        db,
        user=current_user,
        action="stock_category.create",
        target_type="stock_category",
        target_id=cat.id,
        payload={"name": cat.name},
        request=request,
    )
    return _to_category_read(cat)


@router.get("/categories", response_model=list[CategoryRead])
def list_categories(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Any:
    cats = db.query(StockCategory).order_by(StockCategory.created_at.desc()).all()
    return [_to_category_read(c) for c in cats]


@router.patch("/categories/{category_id}", response_model=CategoryRead)
def update_category(
    category_id: int,
    payload: CategoryUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    cat = db.get(StockCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="栏目不存在")

    update_data = payload.model_dump(exclude_unset=True)
    if "name" in update_data and update_data["name"] is not None:
        cat.name = update_data["name"].strip()
    if "description" in update_data:
        cat.description = update_data["description"]
    if "official_url" in update_data:
        cat.official_url = update_data["official_url"]

    db.commit()
    db.refresh(cat)
    add_audit_entry(
        db,
        user=current_user,
        action="stock_category.update",
        target_type="stock_category",
        target_id=category_id,
        payload={"name": cat.name},
        request=request,
    )
    return _to_category_read(cat)


# ── Image routes ──────────────────────────────────────────────────────────────

ALLOWED_IMAGE_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@router.post("/images", response_model=StockImageRead, status_code=201)
async def upload_image(
    request: Request,
    category_id: int,
    tags: str = "",
    description: str | None = None,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    cat = db.get(StockCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="栏目不存在")

    content_type = file.content_type or "application/octet-stream"
    if content_type not in ALLOWED_IMAGE_MIME:
        raise HTTPException(status_code=415, detail="仅支持 JPEG / PNG / WebP / GIF")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="文件为空")

    filename = file.filename or f"{uuid.uuid4().hex}.bin"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    key = f"{uuid.uuid4().hex}.{ext}"
    width, height = _guess_image_size(data)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    try:
        minio_store.upload_image(cat.bucket_name, key, data, content_type)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"上传失败: {exc}") from exc

    img = StockImage(
        category_id=category_id,
        minio_key=key,
        filename=filename,
        description=description,
        tags=tag_list,
        width=width,
        height=height,
    )
    db.add(img)
    db.commit()
    db.refresh(img)
    add_audit_entry(
        db,
        user=current_user,
        action="stock_image.create",
        target_type="stock_image",
        target_id=img.id,
        payload={"category_id": category_id, "filename": filename},
        request=request,
    )
    return _to_image_read(img)


@router.get("/images", response_model=list[StockImageRead])
def list_images(
    category_id: int | None = None,
    tag: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Any:
    q = db.query(StockImage)
    if category_id is not None:
        q = q.filter(StockImage.category_id == category_id)
    if tag:
        q = q.filter(StockImage.tags.contains([tag]))
    images = q.order_by(StockImage.created_at.desc()).all()
    return [_to_image_read(img) for img in images]


@files_router.get("/{image_id}/file")
def serve_image_file(
    image_id: int,
    db: Session = Depends(get_db),
) -> Response:
    """代理返回 MinIO 中的图片文件。无需登录（嵌入文章正文后需公开可访问）。"""
    img = db.get(StockImage, image_id)
    if img is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    cat = db.get(StockCategory, img.category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="栏目不存在")
    try:
        data = minio_store.get_object_bytes(cat.bucket_name, img.minio_key)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MinIO 读取失败: {exc}") from exc

    ext = img.minio_key.rsplit(".", 1)[-1].lower() if "." in img.minio_key else ""
    mime_map = {"png": "image/png", "webp": "image/webp", "gif": "image/gif", "jpg": "image/jpeg", "jpeg": "image/jpeg"}
    content_type = mime_map.get(ext, "image/jpeg")

    return Response(content=data, media_type=content_type)


@router.delete("/images/{image_id}", status_code=204)
def delete_image(
    image_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    img = db.get(StockImage, image_id)
    if img is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    img_filename = img.filename
    cat = db.get(StockCategory, img.category_id)
    if cat:
        try:
            minio_store.delete_object(cat.bucket_name, img.minio_key)
        except Exception:
            pass
    db.delete(img)
    db.commit()
    add_audit_entry(
        db,
        user=current_user,
        action="stock_image.delete",
        target_type="stock_image",
        target_id=image_id,
        payload={"filename": img_filename},
        request=request,
    )


class ImageUpdate(BaseModel):
    tags: str | None = None
    description: str | None = None


@router.patch("/images/{image_id}", response_model=StockImageRead)
def update_image(
    image_id: int,
    payload: ImageUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    img = db.get(StockImage, image_id)
    if img is None:
        raise HTTPException(status_code=404, detail="图片不存在")

    changed_fields: list[str] = []
    if payload.tags is not None:
        img.tags = [t.strip() for t in payload.tags.split(",") if t.strip()]
        changed_fields.append("tags")
    if payload.description is not None:
        img.description = payload.description
        changed_fields.append("description")
    db.commit()
    db.refresh(img)
    add_audit_entry(
        db,
        user=current_user,
        action="stock_image.update",
        target_type="stock_image",
        target_id=image_id,
        payload={"changed_fields": changed_fields},
        request=request,
    )
    return _to_image_read(img)
