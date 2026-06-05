"""纯逻辑：节点间数据传递（inputMapping）与跳过条件（condition）。无 DB 依赖。"""

from __future__ import annotations

from typing import Any


def apply_input_mapping(meta: dict | None, upstream: dict[str, Any] | None) -> dict[str, Any]:
    """按 meta.inputMapping 把上游字段拷到目标字段名。meta/mapping/upstream 空则返回 {}。"""
    out: dict[str, Any] = {}
    if not isinstance(meta, dict) or not isinstance(upstream, dict):
        return out
    for m in meta.get("inputMapping") or []:
        if not isinstance(m, dict):
            continue
        src, dst = m.get("from"), m.get("to")
        if src and dst and src in upstream:
            out[dst] = upstream[src]
    return out


def should_skip(meta: dict | None, ctx: dict[str, Any] | None) -> bool:
    """condition 不满足则返回 True（跳过本节点）。无 condition 永不跳过。op∈eq/neq/contains。"""
    if not isinstance(meta, dict):
        return False
    cond = meta.get("condition")
    if not isinstance(cond, dict) or not cond.get("field"):
        return False
    raw = None if not isinstance(ctx, dict) else ctx.get(cond["field"])
    actual = "" if raw is None else str(raw)
    expected = cond.get("value", "")
    op = cond.get("op") or "eq"
    if op == "neq":
        met = actual != expected
    elif op == "contains":
        met = str(expected) in actual
    else:  # eq
        met = actual == expected
    return not met
