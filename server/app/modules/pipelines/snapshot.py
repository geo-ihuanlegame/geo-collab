"""纯逻辑：pipeline_nodes 与快照字典互转。"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 1


def nodes_to_snapshot(nodes: list[Any]) -> dict:
    """已发布节点（按 node_index 顺序传入）转快照字典。"""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "nodes": [
            {
                "node_type": n.node_type,
                "name": n.name,
                "node_index": n.node_index,
                "config": n.config or {},
                "flow_meta": n.flow_meta,
            }
            for n in nodes
        ],
    }


def snapshot_to_node_dicts(snapshot: dict | None) -> list[dict]:
    """快照字典转可用于创建 PipelineNode 的字段字典列表。"""
    if not snapshot:
        return []
    return [
        {
            "node_type": n.get("node_type"),
            "name": n.get("name"),
            "node_index": n.get("node_index"),
            "config": n.get("config") or {},
            "flow_meta": n.get("flow_meta"),
        }
        for n in snapshot.get("nodes") or []
    ]
