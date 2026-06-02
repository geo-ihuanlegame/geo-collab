"""
文章模块 Pydantic schemas + 序列化函数。

合并自：
  - schemas/article.py
  - schemas/article_group.py
  - schemas/asset.py
  - api/serializers.py（to_article_read、to_group_read）
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any

import nh3
from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from server.app.modules.articles.models import Article, ArticleGroup


# ── Asset ────────────────────────────────────────────────────────────────────


class AssetRead(BaseModel):
    id: str
    filename: str
    ext: str
    mime_type: str
    size: int
    sha256: str
    storage_key: str  # 相对 data_dir 的存储路径
    width: int | None
    height: int | None
    created_at: datetime
    url: str  # 可访问的 API URL

    model_config = {"from_attributes": True}


# ── Article ──────────────────────────────────────────────────────────────────


class ArticleBodyAssetRead(BaseModel):
    asset_id: str
    position: int
    editor_node_id: str | None = None  # Tiptap 编辑器节点 ID


class ArticleBase(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    author: str | None = Field(default=None, max_length=200)
    cover_asset_id: str | None = None
    content_json: dict[str, Any] = Field(default_factory=dict)
    content_html: str = ""
    plain_text: str = ""
    word_count: int = 0
    status: str = "draft"

    @field_validator("content_html", mode="before")
    @classmethod
    def sanitize_content_html(cls, v: str) -> str:
        if not v:
            return v
        allowed_tags = nh3.ALLOWED_TAGS | {"img", "figure", "figcaption", "span"}
        return nh3.clean(v, tags=allowed_tags)


class ArticleCreate(ArticleBase):
    client_request_id: str | None = Field(default=None, max_length=80)


class ArticleUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    author: str | None = Field(default=None, max_length=200)
    cover_asset_id: str | None = None
    content_json: dict[str, Any] | None = None
    content_html: str | None = None
    plain_text: str | None = None
    word_count: int | None = None
    status: str | None = None
    version: int | None = Field(default=None, ge=1)
    stock_category_id: int | None = None
    stock_category_ids: list[int] = Field(default_factory=list)

    @field_validator("content_html", mode="before")
    @classmethod
    def sanitize_content_html_update(cls, v: str | None) -> str | None:
        if not v:
            return v
        allowed_tags = nh3.ALLOWED_TAGS | {"img", "figure", "figcaption", "span"}
        return nh3.clean(v, tags=allowed_tags)


class ArticleCoverUpdate(BaseModel):
    cover_asset_id: str | None = None
    version: int | None = Field(default=None, ge=1)


class ArticleListRead(BaseModel):
    id: int
    title: str
    author: str | None
    cover_asset_id: str | None
    word_count: int
    status: str
    version: int
    published_count: int = 0
    created_at: datetime
    updated_at: datetime


class ArticleRead(BaseModel):
    id: int
    title: str
    author: str | None
    cover_asset_id: str | None
    content_json: dict[str, Any]
    content_html: str
    plain_text: str
    word_count: int
    status: str
    version: int
    body_assets: list[ArticleBodyAssetRead]
    published_count: int = 0  # 成功发布次数
    stock_category_id: int | None = None
    stock_category_ids: list[int] = Field(default_factory=list)
    ai_checking: bool = False
    ai_format_error: str | None = None
    created_at: datetime
    updated_at: datetime


# ── ArticleGroup ─────────────────────────────────────────────────────────────


class ArticleGroupBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class ArticleGroupCreate(ArticleGroupBase):
    pass


class ArticleGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    version: int | None = Field(default=None, ge=1)


class ArticleGroupItemInput(BaseModel):
    article_id: int
    sort_order: int | None = None


class ArticleGroupItemsUpdate(BaseModel):
    items: list[ArticleGroupItemInput]
    version: int | None = Field(default=None, ge=1)


class ArticleGroupItemRead(BaseModel):
    article_id: int
    sort_order: int


class ArticleGroupRead(BaseModel):
    id: int
    name: str
    description: str | None
    version: int
    items: list[ArticleGroupItemRead]
    created_at: datetime
    updated_at: datetime


# ── 序列化函数（原 api/serializers.py）──────────────────────────────────────


def to_article_read(article: "Article", published_count: int = 0) -> ArticleRead:
    from server.app.modules.articles.parser import loads_content_json  # 避免循环 import

    body_assets = sorted(article.body_assets, key=lambda item: item.position)
    return ArticleRead(
        id=article.id,
        title=article.title,
        author=article.author,
        cover_asset_id=article.cover_asset_id,
        content_json=loads_content_json(article.content_json),
        content_html=article.content_html,
        plain_text=article.plain_text,
        word_count=article.word_count,
        status=article.status,
        version=article.version,
        published_count=published_count,
        body_assets=[
            ArticleBodyAssetRead(
                asset_id=item.asset_id,
                position=item.position,
                editor_node_id=item.editor_node_id,
            )
            for item in body_assets
        ],
        stock_category_id=article.stock_category_id,
        stock_category_ids=[sc.id for sc in (article.stock_categories or [])],
        created_at=article.created_at,
        updated_at=article.updated_at,
        ai_checking=article.ai_checking,
        ai_format_error=article.ai_format_error,
    )


def to_group_read(group: "ArticleGroup") -> ArticleGroupRead:
    items = sorted(group.items, key=lambda item: item.sort_order)
    return ArticleGroupRead(
        id=group.id,
        name=group.name,
        description=group.description,
        version=group.version,
        items=[
            ArticleGroupItemRead(
                article_id=item.article_id,
                sort_order=item.sort_order,
            )
            for item in items
        ],
        created_at=group.created_at,
        updated_at=group.updated_at,
    )
