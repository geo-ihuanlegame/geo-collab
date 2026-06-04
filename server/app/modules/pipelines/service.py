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


def create_pipeline(db: Session, *, user_id: int, name: str, description: str | None) -> Pipeline:
    if not name or not name.strip():
        raise ValidationError("名称不能为空")
    p = Pipeline(user_id=user_id, name=name.strip(), description=description, has_draft=False)
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


def patch_pipeline(
    db: Session, p: Pipeline, *, name: str | None, description: str | None
) -> Pipeline:
    if name is not None:
        if not name.strip():
            raise ValidationError("名称不能为空")
        p.name = name.strip()
    if description is not None:
        p.description = description
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
