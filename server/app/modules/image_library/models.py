"""图片库模块 ORM 模型（原 models/stock_image.py）。"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.app.core.time import utcnow
from server.app.db.base import Base


class StockCategory(Base):
    """图库栏目（分桶）。一个栏目对应一个 MinIO bucket（bucket_name 唯一）。

    删栏目级联删图片记录（cascade），但 MinIO 里的对象需调用方另行清理。
    """

    __tablename__ = "stock_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    bucket_name: Mapped[str] = mapped_column(String(63), unique=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    official_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    images = relationship("StockImage", back_populates="category", cascade="all, delete-orphan")


class StockImage(Base):
    """图库单图。minio_key 是其在所属栏目 bucket 内的对象 key（全局唯一）。"""

    __tablename__ = "stock_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("stock_categories.id"), index=True)
    minio_key: Mapped[str] = mapped_column(String(500), unique=True)
    filename: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    category = relationship("StockCategory", back_populates="images")
