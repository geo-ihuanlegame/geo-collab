"""MCP-facing 端点的细化异常 → HTTPException 转换。

GEO 全局异常 handler（main.py 的 catch-all `Exception`）把所有未捕获异常
抹成 "500 服务器内部错误"——MCP 客户端 (Claude Code Loop) 拿到的是黑盒，
无法自治判断 retry / 切模型 / 告警。

本 helper 在 MCP-facing endpoint 内**直接抛 HTTPException**，绕过全局 handler，
把异常类名 + 上游消息塞进 detail。完整 traceback 仍由本模块 logger.exception
落到 server 日志（不丢诊断信息）。

分桶规则（按异常的 __module__ 顶级前缀）：
- litellm / httpx / openai / anthropic → 502 BAD_GATEWAY（上游问题）
- 其它 → 500 INTERNAL_SERVER_ERROR（本服务问题）

detail 截断到 500 字符——超长 prompt / response 不灌进 MCP 通道。
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

logger = logging.getLogger(__name__)

_UPSTREAM_MODULE_PREFIXES: frozenset[str] = frozenset({"litellm", "httpx", "openai", "anthropic"})
_DETAIL_MESSAGE_MAX = 500


def classify_status(exc: BaseException) -> int:
    """按异常来源模块分桶。LLM/HTTP 上游 → 502；其它 → 500。"""
    module = (type(exc).__module__ or "").split(".", 1)[0]
    if module in _UPSTREAM_MODULE_PREFIXES:
        return 502
    return 500


def _format_detail(exc: BaseException) -> str:
    msg = str(exc) or repr(exc)
    if len(msg) > _DETAIL_MESSAGE_MAX:
        msg = msg[: _DETAIL_MESSAGE_MAX - 1] + "…"
    return f"{type(exc).__name__}: {msg}"


def mcp_exception_response(exc: BaseException, *, context: str) -> HTTPException:
    """把任意异常封成可观测的 HTTPException，并把完整 traceback 写日志。

    Args:
        exc: 待封装的异常。
        context: 一句话上下文，仅用于日志（不会进 detail），如
            "compose_one qid=2 tpl=1"。

    返回的 HTTPException 由调用方 `raise` 抛出——会被 FastAPI 内置 handler
    序列化为 `{"detail": "<ExceptionClass>: <truncated message>"}`，绕过 main.py
    的全局 500 handler。
    """
    logger.exception("MCP-facing %s failed", context, exc_info=exc)
    return HTTPException(
        status_code=classify_status(exc),
        detail=_format_detail(exc),
    )
