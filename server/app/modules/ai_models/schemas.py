"""AI 模型注册表 Pydantic schema。

api_key_env 只是环境变量"名"、回传安全；api_key 本体绝不进任何 schema。
scope 非法 → 字段校验失败（FastAPI 转 422，属请求校验、非 service 业务异常）。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from server.app.modules.ai_models.models import SCOPES


class AiModelBase(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    model: str = Field(default="", max_length=200)
    scope: str
    base_url: str | None = Field(default=None, max_length=300)
    api_key_env: str | None = Field(default=None, max_length=80)
    is_enabled: bool = True
    is_default: bool = False
    sort_order: int = 0

    @field_validator("scope")
    @classmethod
    def _validate_scope(cls, v: str) -> str:
        if v not in SCOPES:
            raise ValueError(f"scope 必须是 {SCOPES} 之一")
        return v


class AiModelCreate(AiModelBase):
    pass


class AiModelUpdate(BaseModel):
    """PATCH 语义：仅传入的字段被更新（未传 = 不动）。"""

    label: str | None = Field(default=None, min_length=1, max_length=100)
    model: str | None = Field(default=None, max_length=200)
    scope: str | None = None
    base_url: str | None = Field(default=None, max_length=300)
    api_key_env: str | None = Field(default=None, max_length=80)
    is_enabled: bool | None = None
    is_default: bool | None = None
    sort_order: int | None = None

    @field_validator("scope")
    @classmethod
    def _validate_scope(cls, v: str | None) -> str | None:
        if v is not None and v not in SCOPES:
            raise ValueError(f"scope 必须是 {SCOPES} 之一")
        return v


class AiModelRead(AiModelBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
