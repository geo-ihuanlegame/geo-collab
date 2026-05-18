from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.app.core.time import utcnow
from server.app.db.base import Base


# 资源文件：上传的图片（封面/正文），以 SHA256 前缀为 ID
class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # uuid 作为主键
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(500))
    ext: Mapped[str] = mapped_column(String(30))
    mime_type: Mapped[str] = mapped_column(String(100), index=True)
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)  # 文件内容哈希，用于去重
    storage_key: Mapped[str] = mapped_column(String(500), unique=True)  # 相对 data_dir 的存储路径
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    article_body_links = relationship("ArticleBodyAsset", back_populates="asset")
    task_logs = relationship("TaskLog", back_populates="screenshot_asset")
