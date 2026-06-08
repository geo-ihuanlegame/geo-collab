"""节点注册表与运行上下文：节点类型按 register(node_type, handler) 注册（与 driver 同模式），
执行器用 get_handler 按类型取处理函数。各内置节点模块在导入时调 register；nodes/__init__.py 触发导入。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from server.app.shared.errors import ValidationError


@dataclass
class NodeRunContext:
    session_factory: Callable[[], Any]
    user_id: int
    config: dict
    inputs: dict  # 经 flow_meta inputMapping 注入
    upstream: dict  # 上游累积 context（node_index -> output 的合并视图）
    # 预留：节点可直接读全量上游输出；当前内置节点只用 inputs


@dataclass
class NodeResult:
    output: dict = field(default_factory=dict)
    article_ids: list[int] = field(default_factory=list)


NodeHandler = Callable[[NodeRunContext], NodeResult]
_REGISTRY: dict[str, NodeHandler] = {}


def register(node_type: str, handler: NodeHandler) -> None:
    _REGISTRY[node_type] = handler


def get_handler(node_type: str) -> NodeHandler:
    handler = _REGISTRY.get(node_type)
    if handler is None:
        raise ValidationError(f"未知节点类型: {node_type}")
    return handler


def registered_types() -> list[str]:
    return sorted(_REGISTRY.keys())
