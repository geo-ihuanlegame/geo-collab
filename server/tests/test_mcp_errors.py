"""mcp_errors helper：分桶 + detail 格式 + 日志副作用。

纯函数单测，无 DB / 无 build_test_app 依赖——CI 与本地都可裸跑。
"""

from __future__ import annotations

import logging

import pytest

from server.app.core.mcp_errors import (
    _DETAIL_MESSAGE_MAX,
    classify_status,
    mcp_exception_response,
)

# ─── classify_status ────────────────────────────────────────────────────────


class _FakeLiteLLMError(Exception):
    pass


_FakeLiteLLMError.__module__ = "litellm.exceptions"


class _FakeHttpxError(Exception):
    pass


_FakeHttpxError.__module__ = "httpx._exceptions"


class _FakeOpenAIError(Exception):
    pass


_FakeOpenAIError.__module__ = "openai.errors"


class _FakeAnthropicError(Exception):
    pass


_FakeAnthropicError.__module__ = "anthropic._exceptions"


@pytest.mark.parametrize(
    "exc_cls",
    [_FakeLiteLLMError, _FakeHttpxError, _FakeOpenAIError, _FakeAnthropicError],
)
def test_classify_status_upstream_modules_map_to_502(exc_cls):
    assert classify_status(exc_cls("boom")) == 502


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("simulated db boom"),
        ValueError("bad payload"),
        KeyError("missing"),
        Exception("generic"),
    ],
)
def test_classify_status_other_exceptions_map_to_500(exc):
    assert classify_status(exc) == 500


def test_classify_status_handles_missing_module():
    """少数动态构造的异常类 __module__ 可能为空字符串——不应炸，回落 500。"""

    class _NoModule(Exception):
        pass

    _NoModule.__module__ = ""
    assert classify_status(_NoModule("x")) == 500


# ─── mcp_exception_response ─────────────────────────────────────────────────


def test_mcp_exception_response_includes_class_name_and_message():
    exc = RuntimeError("db connection refused at 127.0.0.1:3306")
    http_exc = mcp_exception_response(exc, context="compose_one qid=1 tpl=1")
    assert http_exc.status_code == 500
    assert http_exc.detail == "RuntimeError: db connection refused at 127.0.0.1:3306"


def test_mcp_exception_response_routes_litellm_to_502():
    exc = _FakeLiteLLMError("AuthenticationError: 401 from upstream")
    http_exc = mcp_exception_response(exc, context="compose_one")
    assert http_exc.status_code == 502
    assert http_exc.detail.startswith("_FakeLiteLLMError: ")
    assert "AuthenticationError" in http_exc.detail


def test_mcp_exception_response_truncates_long_message():
    """超长上游响应消息（如 LiteLLM 把整个 response body 塞进 message）截断到上限。"""
    long_msg = "x" * (_DETAIL_MESSAGE_MAX * 2)
    exc = RuntimeError(long_msg)
    http_exc = mcp_exception_response(exc, context="compose_one")
    # detail 长度 = "RuntimeError: " 前缀 + 截断后消息
    assert len(http_exc.detail) <= len("RuntimeError: ") + _DETAIL_MESSAGE_MAX
    assert http_exc.detail.endswith("…")


def test_mcp_exception_response_falls_back_to_repr_for_empty_message():
    """有些异常 str() 为空（如 NotImplementedError()），detail 应回落到 repr。"""
    exc = NotImplementedError()
    http_exc = mcp_exception_response(exc, context="x")
    assert http_exc.detail.startswith("NotImplementedError: ")
    # str(exc) == "" 时 detail 不能只剩 "NotImplementedError: "（前缀后是空）
    assert http_exc.detail != "NotImplementedError: "


def test_mcp_exception_response_writes_traceback_to_logger(caplog):
    """完整 traceback 走 logger.exception → 容器日志里仍能看到。"""
    with caplog.at_level(logging.ERROR, logger="server.app.core.mcp_errors"):
        try:
            raise RuntimeError("test-trace-marker")
        except RuntimeError as exc:
            mcp_exception_response(exc, context="trace-test ctx")

    assert any("trace-test ctx" in r.message for r in caplog.records)
    # caplog 的 exc_info 会自带 traceback 信息
    exc_records = [r for r in caplog.records if r.exc_info is not None]
    assert exc_records, "logger.exception 应携带 exc_info"
