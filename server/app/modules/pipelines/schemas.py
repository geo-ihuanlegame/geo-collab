"""Pipeline API 的 Pydantic 入参 / 出参模型（创建 / 局部更新 / 读出 / 草稿 / 版本 / 运行 / 运行日志分页）。"""

from __future__ import annotations

from datetime import datetime, time

from pydantic import BaseModel, ConfigDict


class PipelineCreate(BaseModel):
    name: str
    description: str | None = None
    type: str = "general"
    tags: list[str] = []
    ignore_exception: bool = False
    is_enabled: bool = True
    schedule_kind: str = "none"
    schedule_minute: int | None = None
    schedule_hour: int | None = None
    schedule_weekday: int | None = None
    window_start: time | None = None
    window_end: time | None = None


class PipelinePatch(BaseModel):
    name: str | None = None
    description: str | None = None
    type: str | None = None
    tags: list[str] | None = None
    ignore_exception: bool | None = None
    is_enabled: bool | None = None
    schedule_kind: str | None = None
    schedule_minute: int | None = None
    schedule_hour: int | None = None
    schedule_weekday: int | None = None
    window_start: time | None = None
    window_end: time | None = None


class NodeRead(BaseModel):
    node_type: str
    name: str
    node_index: int
    config: dict
    flow_meta: dict | None = None


class PipelineRead(BaseModel):
    id: int
    name: str
    description: str | None
    has_draft: bool
    is_running: bool = False
    type: str = "general"
    tags: list[str] = []
    ignore_exception: bool = False
    is_enabled: bool = True
    schedule_kind: str = "none"
    schedule_minute: int | None = None
    schedule_hour: int | None = None
    schedule_weekday: int | None = None
    window_start: time | None = None
    window_end: time | None = None
    last_scheduled_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    nodes: list[NodeRead] = []
    model_config = ConfigDict(from_attributes=True)


class DraftSave(BaseModel):
    snapshot: dict


class PublishRequest(BaseModel):
    remark: str | None = None


class VersionRead(BaseModel):
    id: int
    pipeline_id: int
    version_no: int
    remark: str | None
    created_by: int
    created_at: datetime
    snapshot: dict | None = None
    model_config = ConfigDict(from_attributes=True)


class RunRead(BaseModel):
    id: int
    pipeline_id: int
    status: str
    article_ids: list = []
    node_results: dict = {}
    error_message: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    model_config = ConfigDict(from_attributes=True)


class RunLogRow(BaseModel):
    batch: int
    run_status: str
    step: int
    task_name: str
    level: str  # "ERROR" | "INFO"（错误 | 信息）
    message: str
    duration_ms: int | None = None  # 该节点执行耗时（来自 node_results 富化），未记则 None
    time: datetime | None = None


class RunLogPage(BaseModel):
    items: list[RunLogRow]
    total: int
    page: int
    page_size: int
