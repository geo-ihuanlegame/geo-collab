from datetime import datetime
from typing import Any

import nh3
from pydantic import BaseModel, Field, field_validator


# 文章正文中的图片信息
class ArticleBodyAssetRead(BaseModel):
    asset_id: str
    position: int
    editor_node_id: str | None = None  # Tiptap 编辑器节点 ID


# 文章基础信息（创建时使用）
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


# 文章更新请求（所有字段可选）
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

    @field_validator("content_html", mode="before")
    @classmethod
    def sanitize_content_html_update(cls, v: str | None) -> str | None:
        if not v:
            return v
        allowed_tags = nh3.ALLOWED_TAGS | {"img", "figure", "figcaption", "span"}
        return nh3.clean(v, tags=allowed_tags)


# 仅更新文章封面
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


# 文章完整响应体
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
    ai_checking: bool = False
    ai_format_error: str | None = None
    created_at: datetime
    updated_at: datetime

