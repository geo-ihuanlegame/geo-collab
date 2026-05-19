"""分块上传资源的 API 路由。"""
import hashlib
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from server.app.core.security import get_current_user
from server.app.db.session import get_db
from server.app.models import User
from server.app.modules.articles.asset_Store import (
    _create_asset_from_path,
    normalize_ext,
    guess_image_size,
)
from server.app.modules.articles.chunked_upload import (
    CHUNK_SIZE,
    get_upload_manager,
)
from server.app.core.config import ALLOWED_MAGIC

router = APIRouter()


@router.post("/upload-start")
async def start_chunked_upload(
    total_size: int,
    file_hash: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """初始化分块上传。

    Args:
        total_size: 文件总大小（字节）
        file_hash: 文件的 SHA256 哈希（客户端计算）

    Returns:
        {
            "upload_id": "...",
            "chunk_size": 3145728,
            "chunk_count": 4
        }
    """
    from server.app.core.config import MAX_ASSET_BYTES

    if total_size > MAX_ASSET_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds {MAX_ASSET_BYTES // (1024 * 1024)}MB limit",
        )

    manager = get_upload_manager()
    session = manager.init_session(total_size, file_hash)

    return {
        "upload_id": session.upload_id,
        "chunk_size": CHUNK_SIZE,
        "chunk_count": session.chunk_count,
    }


@router.post("/upload-chunk/{upload_id}")
async def upload_chunk(
    upload_id: str,
    chunk_index: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """上传单个分块。

    Args:
        upload_id: 分块上传会话 ID
        chunk_index: 分块索引（0-based）
        file: 分块数据

    Returns:
        {"status": "ok"}
    """
    manager = get_upload_manager()
    session = manager.get_session(upload_id)

    if not session:
        raise HTTPException(status_code=404, detail="Upload session not found")

    if chunk_index < 0 or chunk_index >= session.chunk_count:
        raise HTTPException(status_code=400, detail="Invalid chunk index")

    # 读取分块数据
    chunk_data = await file.read()

    # 验证分块大小（最后一个分块可能更小）
    if chunk_index < session.chunk_count - 1:
        if len(chunk_data) != CHUNK_SIZE:
            raise HTTPException(status_code=400, detail="Invalid chunk size")
    else:
        # 最后一个分块
        expected_last_size = session.total_size - (session.chunk_count - 1) * CHUNK_SIZE
        if len(chunk_data) != expected_last_size:
            raise HTTPException(status_code=400, detail="Invalid last chunk size")

    # 保存分块
    await manager.save_chunk(upload_id, chunk_index, chunk_data)

    return {"status": "ok"}


@router.post("/upload-status/{upload_id}")
async def get_upload_status(
    upload_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """获取上传进度。

    Returns:
        {
            "chunk_count": 4,
            "uploaded_chunks": [0, 1, 2],
            "is_complete": false
        }
    """
    manager = get_upload_manager()
    session = manager.get_session(upload_id)

    if not session:
        raise HTTPException(status_code=404, detail="Upload session not found")

    uploaded = manager.get_uploaded_chunks(upload_id)

    return {
        "chunk_count": session.chunk_count,
        "uploaded_chunks": sorted(list(uploaded)),
        "is_complete": manager.is_complete(upload_id),
    }


@router.post("/upload-complete/{upload_id}")
async def complete_chunked_upload(
    upload_id: str,
    filename: str,
    content_type: str = "application/octet-stream",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """完成分块上传，合并所有分块并创建资源。

    Args:
        upload_id: 分块上传会话 ID
        filename: 文件名
        content_type: 文件 MIME 类型

    Returns:
        资源信息 (Asset)
    """
    manager = get_upload_manager()
    session = manager.get_session(upload_id)

    if not session:
        raise HTTPException(status_code=404, detail="Upload session not found")

    if not manager.is_complete(upload_id):
        raise HTTPException(status_code=400, detail="Upload not complete")

    try:
        import asyncio

        # 合并分块（在线程池中执行以避免阻塞）
        loop = asyncio.get_event_loop()
        merged_path = await loop.run_in_executor(
            None, manager.merge_chunks, upload_id
        )

        # 读取文件内容用于格式验证
        file_data = merged_path.read_bytes()

        # 验证文件格式
        if not any(file_data.startswith(m) for m in ALLOWED_MAGIC):
            merged_path.unlink()
            raise HTTPException(status_code=415, detail="Unsupported file type")

        # 创建资源
        ext = normalize_ext(filename, content_type, file_data)
        width, height = guess_image_size(file_data)

        stored = await loop.run_in_executor(
            None,
            _create_asset_from_path,
            db,
            current_user.id,
            merged_path,
            filename,
            content_type,
            session.file_hash,
            session.total_size,
            ext,
            width,
            height,
        )

        db.refresh(stored.asset)
        db.commit()

        from server.app.api.routes.assets import to_asset_read

        return to_asset_read(stored.asset).model_dump()

    except Exception as e:
        manager.cleanup_session(upload_id)
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        # 清理会话
        manager.cleanup_session(upload_id)
