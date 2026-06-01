"""分块上传管理器 — 支持上传大文件到临时位置，然后合并。"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

_logger = logging.getLogger(__name__)

import aiofiles

from server.app.core.paths import get_data_dir
from server.app.core.config import ALLOWED_MAGIC


CHUNK_SIZE = 3 * 1024 * 1024  # 3MB
STREAM_BUFFER_SIZE = 64 * 1024  # 64KB buffer for streaming I/O
MAGIC_BYTES_CHECK_SIZE = 512  # bytes to check for format validation


@dataclass
class UploadSession:
    """分块上传会话信息。"""
    upload_id: str
    total_size: int
    chunk_count: int
    temp_dir: Path

    def get_chunk_path(self, chunk_index: int) -> Path:
        return self.temp_dir / f"chunk_{chunk_index}"

    def get_metadata_path(self) -> Path:
        return self.temp_dir / "metadata.txt"


class ChunkedUploadManager:
    """管理分块上传的生命周期。"""

    def __init__(self):
        self.sessions: dict[str, UploadSession] = {}
        self.sessions_dir = get_data_dir() / ".uploads"
        self._cleanup_orphaned_uploads()

    def init_session(self, total_size: int) -> UploadSession:
        """初始化一个新的分块上传会话。"""
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        upload_id = uuid.uuid4().hex
        temp_dir = self.sessions_dir / upload_id
        temp_dir.mkdir(parents=True, exist_ok=True)

        chunk_count = (total_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        session = UploadSession(
            upload_id=upload_id,
            total_size=total_size,
            chunk_count=chunk_count,
            temp_dir=temp_dir,
        )

        self.sessions[upload_id] = session

        metadata = {
            "upload_id": session.upload_id,
            "total_size": session.total_size,
            "chunk_count": session.chunk_count,
        }
        (temp_dir / "session.json").write_text(json.dumps(metadata))

        return session

    def _cleanup_orphaned_uploads(self) -> None:
        if not self.sessions_dir.exists():
            return
        import shutil
        now = time.time()
        max_age = 86400  # 24 hours
        for entry in self.sessions_dir.iterdir():
            if not entry.is_dir():
                continue
            session_file = entry / "session.json"
            if session_file.exists():
                mtime = session_file.stat().st_mtime
                if now - mtime < max_age:
                    try:
                        metadata = json.loads(session_file.read_text())
                        session = UploadSession(
                            upload_id=metadata["upload_id"],
                            total_size=metadata["total_size"],
                            chunk_count=metadata["chunk_count"],
                            temp_dir=entry,
                        )
                        self.sessions[metadata["upload_id"]] = session
                        continue
                    except Exception:
                        pass
            shutil.rmtree(entry, ignore_errors=True)
            _logger.info("Cleaned up orphaned upload dir: %s", entry.name)

    def get_session(self, upload_id: str) -> UploadSession | None:
        """获取上传会话。"""
        return self.sessions.get(upload_id)

    async def save_chunk(self, upload_id: str, chunk_index: int, data: bytes) -> None:
        """保存单个分块。"""
        session = self.sessions.get(upload_id)
        if not session:
            raise ValueError(f"Upload session {upload_id} not found")

        chunk_path = session.get_chunk_path(chunk_index)
        async with aiofiles.open(str(chunk_path), "wb") as f:
            await f.write(data)

    def get_uploaded_chunks(self, upload_id: str) -> set[int]:
        """获取已上传的分块索引。"""
        session = self.sessions.get(upload_id)
        if not session:
            return set()

        uploaded = set()
        for i in range(session.chunk_count):
            if session.get_chunk_path(i).exists():
                uploaded.add(i)
        return uploaded

    def is_complete(self, upload_id: str) -> bool:
        """检查是否所有分块都已上传。"""
        session = self.sessions.get(upload_id)
        if not session:
            return False

        uploaded = self.get_uploaded_chunks(upload_id)
        return len(uploaded) == session.chunk_count

    def merge_chunks(self, upload_id: str) -> tuple[Path, str, bool, str | None]:
        """合并所有分块到单个临时文件，使用流式处理并执行早期格式验证。

        Returns:
            Tuple of (merged_path, sha256_hash, is_valid_format, format_error)
            - merged_path: Path to merged file
            - sha256_hash: Server-computed SHA256 hex digest
            - is_valid_format: True if file magic bytes match ALLOWED_MAGIC
            - format_error: Error message if format validation failed, None otherwise
        """
        session = self.sessions.get(upload_id)
        if not session:
            raise ValueError(f"Upload session {upload_id} not found")

        merged_path = session.temp_dir / "merged_file"
        sha256 = hashlib.sha256()
        total = 0
        magic_bytes_buffer = bytearray()
        is_valid_format = False
        format_error = None
        format_checked = False

        with open(merged_path, "wb") as out:
            for i in range(session.chunk_count):
                chunk_path = session.get_chunk_path(i)
                if not chunk_path.exists():
                    raise ValueError(f"Missing chunk {i} in upload {upload_id}")

                # Stream-read each chunk file in small buffers
                with open(chunk_path, "rb") as chunk_file:
                    while True:
                        buffer = chunk_file.read(STREAM_BUFFER_SIZE)
                        if not buffer:
                            break

                        # Collect magic bytes for format validation (first 512 bytes)
                        if not format_checked:
                            bytes_needed = MAGIC_BYTES_CHECK_SIZE - len(magic_bytes_buffer)
                            if bytes_needed > 0:
                                magic_bytes_buffer.extend(buffer[:bytes_needed])

                            # Check if we have enough data or reached end of file
                            will_have_checked = (
                                len(magic_bytes_buffer) >= MAGIC_BYTES_CHECK_SIZE
                                or total + len(buffer) >= session.total_size
                            )
                            if will_have_checked:
                                is_valid_format = any(
                                    bytes(magic_bytes_buffer).startswith(m)
                                    for m in ALLOWED_MAGIC
                                )
                                if not is_valid_format:
                                    format_error = "Unsupported file type"
                                format_checked = True

                        # Write to output and update hash
                        out.write(buffer)
                        sha256.update(buffer)
                        total += len(buffer)

        if total != session.total_size:
            merged_path.unlink()
            raise ValueError("Merged file size mismatch")

        # 删除分块文件
        for i in range(session.chunk_count):
            session.get_chunk_path(i).unlink()

        return merged_path, sha256.hexdigest(), is_valid_format, format_error

    def cleanup_session(self, upload_id: str) -> None:
        """清理上传会话（删除临时文件）。"""
        session = self.sessions.pop(upload_id, None)
        if not session:
            return

        import shutil

        shutil.rmtree(session.temp_dir, ignore_errors=True)


# 全局实例
_upload_manager: ChunkedUploadManager | None = None


def get_upload_manager() -> ChunkedUploadManager:
    """获取全局分块上传管理器。"""
    global _upload_manager
    if _upload_manager is None:
        _upload_manager = ChunkedUploadManager()
    return _upload_manager
