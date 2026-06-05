from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.app.modules.pipelines.models import (
    Pipeline,
    PipelineNode,
    PipelineRun,
    PipelineVersion,
)
from server.app.modules.pipelines.snapshot import nodes_to_snapshot, snapshot_to_node_dicts
from server.app.shared.errors import ClientError, ValidationError

VALID_AGENT_TYPES = {"generation", "distribution", "general"}
VALID_SCHEDULE_KINDS = {"none", "hourly", "daily", "weekly"}


def _dedup_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        s = t.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def validate_agent_fields(
    *,
    name,
    type,
    tags,
    schedule_kind,
    schedule_minute,
    schedule_hour,
    schedule_weekday,
    window_start,
    window_end,
) -> None:
    if not name or not name.strip():
        raise ValidationError("名称不能为空")
    if len(name.strip()) > 50:
        raise ValidationError("名称长度不能超过 50")
    if type not in VALID_AGENT_TYPES:
        raise ValidationError(f"非法类型: {type}")
    if not isinstance(tags, list) or len(tags) > 5:
        raise ValidationError("标签最多 5 个")
    for t in tags:
        if not isinstance(t, str) or not t.strip():
            raise ValidationError("标签不能为空")
    if schedule_kind not in VALID_SCHEDULE_KINDS:
        raise ValidationError(f"非法调度类型: {schedule_kind}")
    if schedule_kind in ("hourly", "daily", "weekly"):
        if schedule_minute is None or not (0 <= schedule_minute <= 59):
            raise ValidationError("分钟需在 0-59")
    if schedule_kind in ("daily", "weekly"):
        if schedule_hour is None or not (0 <= schedule_hour <= 23):
            raise ValidationError("小时需在 0-23")
    if schedule_kind == "weekly":
        if schedule_weekday is None or not (0 <= schedule_weekday <= 6):
            raise ValidationError("星期需在 0-6（周一=0）")
    if (window_start is None) != (window_end is None):
        raise ValidationError("时间窗起止需同时设置或同时留空")
    if window_start is not None and window_end is not None and not (window_start < window_end):
        raise ValidationError("时间窗起须早于止")


def create_pipeline(
    db: Session,
    *,
    user_id: int,
    name: str,
    description: str | None,
    type: str = "general",
    tags: list[str] | None = None,
    ignore_exception: bool = False,
    is_enabled: bool = True,
    schedule_kind: str = "none",
    schedule_minute: int | None = None,
    schedule_hour: int | None = None,
    schedule_weekday: int | None = None,
    window_start=None,
    window_end=None,
) -> Pipeline:
    tags = tags or []
    validate_agent_fields(
        name=name,
        type=type,
        tags=tags,
        schedule_kind=schedule_kind,
        schedule_minute=schedule_minute,
        schedule_hour=schedule_hour,
        schedule_weekday=schedule_weekday,
        window_start=window_start,
        window_end=window_end,
    )
    tags = _dedup_tags(tags)
    p = Pipeline(
        user_id=user_id,
        name=name.strip(),
        description=description,
        has_draft=False,
        type=type,
        tags=tags,
        ignore_exception=ignore_exception,
        is_enabled=is_enabled,
        schedule_kind=schedule_kind,
        schedule_minute=schedule_minute,
        schedule_hour=schedule_hour,
        schedule_weekday=schedule_weekday,
        window_start=window_start,
        window_end=window_end,
    )
    db.add(p)
    db.flush()
    return p


def get_pipeline(db: Session, pipeline_id: int) -> Pipeline | None:
    return db.get(Pipeline, pipeline_id)


def list_pipelines(db: Session, *, user_id: int, is_admin: bool) -> list[Pipeline]:
    q = select(Pipeline).order_by(Pipeline.id.desc())
    if not is_admin:
        q = q.where(Pipeline.user_id == user_id)
    return list(db.execute(q).scalars().all())


def list_nodes(db: Session, pipeline_id: int) -> list[PipelineNode]:
    q = (
        select(PipelineNode)
        .where(PipelineNode.pipeline_id == pipeline_id)
        .order_by(PipelineNode.node_index.asc())
    )
    return list(db.execute(q).scalars().all())


def patch_pipeline(db: Session, p: Pipeline, *, fields: dict) -> Pipeline:
    """fields = PipelinePatch.model_dump(exclude_unset=True)。只覆盖提供的字段。"""
    merged = {
        "name": p.name,
        "type": p.type,
        "tags": list(p.tags or []),
        "schedule_kind": p.schedule_kind,
        "schedule_minute": p.schedule_minute,
        "schedule_hour": p.schedule_hour,
        "schedule_weekday": p.schedule_weekday,
        "window_start": p.window_start,
        "window_end": p.window_end,
    }
    for k in merged:
        if k in fields and fields[k] is not None:
            merged[k] = fields[k]
    validate_agent_fields(**merged)
    # 应用（含 description / 开关，None=不改）
    settable = [
        "name",
        "description",
        "type",
        "tags",
        "ignore_exception",
        "is_enabled",
        "schedule_kind",
        "schedule_minute",
        "schedule_hour",
        "schedule_weekday",
        "window_start",
        "window_end",
    ]
    for k in settable:
        if k in fields and fields[k] is not None:
            if k == "name":
                setattr(p, k, fields[k].strip())
            elif k == "tags":
                setattr(p, k, _dedup_tags(fields[k]))
            else:
                setattr(p, k, fields[k])
    db.flush()
    return p


def delete_pipeline(db: Session, p: Pipeline) -> None:
    db.query(PipelineNode).filter(PipelineNode.pipeline_id == p.id).delete()
    db.query(PipelineVersion).filter(PipelineVersion.pipeline_id == p.id).delete()
    db.query(PipelineRun).filter(PipelineRun.pipeline_id == p.id).delete()
    db.delete(p)
    db.flush()


def save_draft(db: Session, p: Pipeline, snapshot: dict) -> None:
    p.draft_snapshot = snapshot
    p.has_draft = True
    db.flush()


def discard_draft(db: Session, p: Pipeline) -> None:
    p.draft_snapshot = None
    p.has_draft = False
    db.flush()


def publish_draft(db: Session, p: Pipeline, *, remark: str | None, user_id: int) -> int:
    # 串行化同一 pipeline 的并发发布，避免 version_no 重号
    db.query(Pipeline).filter(Pipeline.id == p.id).with_for_update().first()
    if not p.has_draft or not p.draft_snapshot:
        raise ClientError("没有可发布的草稿")
    node_dicts = snapshot_to_node_dicts(p.draft_snapshot)
    if not node_dicts:
        raise ClientError("草稿内容为空")
    # 重建 live 节点
    db.query(PipelineNode).filter(PipelineNode.pipeline_id == p.id).delete()
    for nd in node_dicts:
        db.add(
            PipelineNode(
                pipeline_id=p.id,
                node_type=nd["node_type"],
                name=nd["name"],
                node_index=nd["node_index"],
                config=nd.get("config") or {},
                flow_meta=nd.get("flow_meta"),
            )
        )
    db.flush()
    # 写版本快照（用 live 节点规范化）
    live = list_nodes(db, p.id)
    next_no = _next_version_no(db, p.id)
    db.add(
        PipelineVersion(
            pipeline_id=p.id,
            version_no=next_no,
            snapshot=nodes_to_snapshot(live),
            remark=remark,
            created_by=user_id,
        )
    )
    p.draft_snapshot = None
    p.has_draft = False
    db.flush()
    return next_no


def _next_version_no(db: Session, pipeline_id: int) -> int:
    rows = (
        db.execute(
            select(PipelineVersion.version_no).where(PipelineVersion.pipeline_id == pipeline_id)
        )
        .scalars()
        .all()
    )
    return (max(rows) if rows else 0) + 1


def list_versions(db: Session, pipeline_id: int) -> list[PipelineVersion]:
    q = (
        select(PipelineVersion)
        .where(PipelineVersion.pipeline_id == pipeline_id)
        .order_by(PipelineVersion.version_no.desc())
    )
    return list(db.execute(q).scalars().all())


def get_version(db: Session, version_id: int) -> PipelineVersion | None:
    return db.get(PipelineVersion, version_id)


def rollback_to_draft(db: Session, p: Pipeline, version: PipelineVersion) -> None:
    p.draft_snapshot = version.snapshot
    p.has_draft = True
    db.flush()
