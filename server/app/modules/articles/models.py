"""
文章模块 ORM 模型。

包含：
  - Article          — 文章（正文三份存储：Tiptap JSON / HTML / 纯文本）
  - ArticleBodyAsset — 正文图片位置关联
  - ArticleGroup     — 文章分组
  - ArticleGroupItem — 分组-文章多对多关联（带排序）
  - Asset            — 上传的资源文件（图片等）
  - Tag / ArticleTag — 文章标签
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.app.core.time import utcnow
from server.app.db.base import Base

# 文章-图库栏目 多对多关联表
article_stock_categories_table = Table(
    "article_stock_categories",
    Base.metadata,
    Column("article_id", Integer, ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "stock_category_id",
        Integer,
        ForeignKey("stock_categories.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    UniqueConstraint("article_id", "stock_category_id", name="uq_article_stock_cat"),
)


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
    webp_storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    webp_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumb_storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    thumb_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    article_body_links = relationship("ArticleBodyAsset", back_populates="asset")
    task_logs = relationship("TaskLog", back_populates="screenshot_asset")


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ArticleTag(Base):
    __tablename__ = "article_tags"

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        CheckConstraint("status in ('draft', 'ready', 'archived')", name="ck_articles_status"),
        CheckConstraint(
            "review_status in ('pending', 'approved')", name="ck_articles_review_status"
        ),
        UniqueConstraint("user_id", "client_request_id", name="uq_articles_user_client_request_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(300), index=True)
    author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cover_asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"), nullable=True)
    content_json: Mapped[str] = mapped_column(Text, default="{}")  # Tiptap 编辑器 JSON
    content_html: Mapped[str] = mapped_column(Text, default="")  # 渲染用 HTML
    plain_text: Mapped[str] = mapped_column(Text, default="")  # 纯文本，用于发布
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(
        String(30), default="draft", index=True
    )  # 状态：draft / ready / archived
    # 审核状态：pending=未审核 / approved=已审核。默认 approved（既有+手工内容视为已审）；
    # AI 方案生成的文章由 scheme_executor 显式置 pending。未过审不可发布。
    review_status: Mapped[str] = mapped_column(
        String(20), default="approved", server_default="approved", index=True
    )
    client_request_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    ai_checking: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0", index=True
    )
    ai_checking_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ai_format_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    stock_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("stock_categories.id"), nullable=True
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    cover_asset = relationship("Asset", foreign_keys=[cover_asset_id])
    stock_category = relationship("StockCategory", foreign_keys=[stock_category_id])
    stock_categories = relationship(
        "StockCategory",
        secondary="article_stock_categories",
        lazy="select",
    )
    body_assets = relationship(
        "ArticleBodyAsset", back_populates="article", cascade="all, delete-orphan"
    )
    group_items = relationship("ArticleGroupItem", back_populates="article")
    publish_records = relationship("PublishRecord", back_populates="article")
    tags = relationship("Tag", secondary="article_tags", lazy="selectin")


class ArticleBodyAsset(Base):
    __tablename__ = "article_body_assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)  # 在正文中的排序位置
    editor_node_id: Mapped[str | None] = mapped_column(String(200), nullable=True)  # Tiptap 节点 ID
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    article = relationship("Article", back_populates="body_assets")
    asset = relationship("Asset", back_populates="article_body_links")


class ArticleGroup(Base):
    __tablename__ = "article_groups"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_article_groups_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    items = relationship("ArticleGroupItem", back_populates="group", cascade="all, delete-orphan")
    publish_tasks = relationship("PublishTask", back_populates="group")


class ArticleGroupItem(Base):
    __tablename__ = "article_group_items"
    __table_args__ = (
        UniqueConstraint("group_id", "article_id", name="uq_article_group_items_group_article"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("article_groups.id"), index=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    group = relationship("ArticleGroup", back_populates="items")
    article = relationship("Article", back_populates="group_items")
