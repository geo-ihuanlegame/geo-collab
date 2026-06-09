"""纯逻辑：节点间数据传递（inputMapping）与跳过条件（condition）。无 DB 依赖。"""

from __future__ import annotations

from typing import Any


def apply_input_mapping(meta: dict | None, upstream: dict[str, Any] | None) -> dict[str, Any]:
    """解析节点入参（ctx.inputs）。

    - 未配置 inputMapping（meta 为空 / 无该键 / 空列表）→ **默认透传整个上游输出**：
      选定上游节点（或默认合并全部上游）即自动把上游字段传给本节点，无需手填同名映射。
      这样"连了上游却没出数据"的常见坑不再出现。
    - 配置了 inputMapping → 只注入映射命中的字段（用于改名 / 筛选），未命中的不透传。

    上游非 dict（无上游）时返回 {}。
    """
    if not isinstance(upstream, dict):
        return {}
    mapping = meta.get("inputMapping") if isinstance(meta, dict) else None
    if not mapping:
        # 无显式映射 → 透传上游（浅拷贝，避免下游处理函数误改上下文中的原始输出）
        return dict(upstream)
    out: dict[str, Any] = {}
    for m in mapping:
        if not isinstance(m, dict):
            continue
        src, dst = m.get("from"), m.get("to")
        if src and dst and src in upstream:
            out[dst] = upstream[src]
    return out


def should_skip(meta: dict | None, ctx: dict[str, Any] | None) -> bool:
    """跳过条件不满足则返回 True（跳过本节点）。无 condition 永不跳过。op∈eq/neq/contains。"""
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
