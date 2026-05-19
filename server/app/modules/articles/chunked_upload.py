"""分块上传管理器 — 支持上传大文件到临时位置，然后合并。"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path

from server.app.core.paths import get_data_dir


CHUNK_SIZE = 3 * 1024 * 1024  # 3MB


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
        return session

    def get_session(self, upload_id: str) -> UploadSession | None:
        """获取上传会话。"""
        return self.sessions.get(upload_id)

    async def save_chunk(self, upload_id: str, chunk_index: int, data: bytes) -> None:
        """保存单个分块。"""
        session = self.sessions.get(upload_id)
        if not session:
            raise ValueError(f"Upload session {upload_id} not found")

        chunk_path = session.get_chunk_path(chunk_index)
        chunk_path.write_bytes(data)

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

    def merge_chunks(self, upload_id: str) -> tuple[Path, str]:
        """合并所有分块到单个临时文件，并返回服务端计算的 SHA256。"""
        session = self.sessions.get(upload_id)
        if not session:
            raise ValueError(f"Upload session {upload_id} not found")

        merged_path = session.temp_dir / "merged_file"
        sha256 = hashlib.sha256()
        total = 0

        with open(merged_path, "wb") as out:
            for i in range(session.chunk_count):
                chunk_path = session.get_chunk_path(i)
                if not chunk_path.exists():
                    raise ValueError(f"Missing chunk {i} in upload {upload_id}")
                data = chunk_path.read_bytes()
                out.write(data)
                sha256.update(data)
                total += len(data)

        if total != session.total_size:
            merged_path.unlink()
            raise ValueError("Merged file size mismatch")

        # 删除分块文件
        for i in range(session.chunk_count):
            session.get_chunk_path(i).unlink()

        return merged_path, sha256.hexdigest()

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
