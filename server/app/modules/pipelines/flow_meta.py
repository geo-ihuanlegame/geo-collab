"""纯逻辑：节点间数据传递（inputMapping）与跳过条件（condition）。无 DB 依赖。"""
from __future__ import annotations

from typing import Any


def apply_input_mapping(meta: dict | None, upstream: dict[str, Any] | None) -> dict[str, Any]:
    """按 meta.inputMapping 把上游字段拷到目标字段名。meta/mapping/upstream 空则返回 {}。"""
    out: dict[str, Any] = {}
    if not meta or not upstream:
        return out
    for m in meta.get("inputMapping") or []:
        src, dst = m.get("from"), m.get("to")
        if src and dst and src in upstream:
            out[dst] = upstream[src]
    return out


def should_skip(meta: dict | None, ctx: dict[str, Any] | None) -> bool:
    """condition 不满足则返回 True（跳过本节点）。无 condition 永不跳过。op∈eq/neq/contains。"""
    if not meta:
        return False
    cond = meta.get("condition")
    if not cond or not cond.get("field"):
        return False
    actual = "" if ctx is None or ctx.get(cond["field"]) is None else str(ctx.get(cond["field"]))
    expected = cond.get("value") or ""
    op = cond.get("op") or "eq"
    if op == "neq":
        met = actual != expected
    elif op == "contains":
        met = expected in actual
    else:  # eq
        met = actual == expected
    return not met
