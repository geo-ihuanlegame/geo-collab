import httpx
import pytest

from server.app.modules.tasks.drivers.base import (
    CommitGuard,
    CommitUncertainError,
    PublishError,
)


def _guard(flag):
    return CommitGuard(mark_pending=lambda: flag.__setitem__("marked", True))


def test_marks_pending_on_enter():
    flag = {"marked": False}
    with _guard(flag).committing():
        assert flag["marked"] is True


def test_success_passes_through():
    flag = {"marked": False}
    with _guard(flag).committing():
        pass  # 无异常


def test_read_timeout_becomes_uncertain():
    flag = {"marked": False}
    with pytest.raises(CommitUncertainError):
        with _guard(flag).committing():
            raise httpx.ReadTimeout("response lost")


def test_connect_error_is_clean_failure():
    """连接从未建立 → 请求从未发出 → 干净失败，原异常透出（可安全重试）。"""
    flag = {"marked": False}
    with pytest.raises(httpx.ConnectError):
        with _guard(flag).committing():
            raise httpx.ConnectError("never connected")


def test_business_errcode_is_clean_failure():
    """服务端回了非空错误码 → 必定未受理 → 原异常透出。"""

    class _ApiErr(PublishError):
        def __init__(self):
            super().__init__("err 40164")
            self.errcode = 40164

    flag = {"marked": False}
    with pytest.raises(_ApiErr):
        with _guard(flag).committing():
            raise _ApiErr()


def test_unknown_exception_defaults_uncertain():
    """无正面「未受理」证据 → 保守判 uncertain（at-most-once）。"""
    flag = {"marked": False}
    with pytest.raises(CommitUncertainError):
        with _guard(flag).committing():
            raise RuntimeError("toutiao click timed out")


def test_wrapped_read_timeout_is_uncertain():
    """外层 errcode=None + __cause__=httpx.ReadTimeout → uncertain（链式穿透，不可重试）。"""
    cause = httpx.ReadTimeout("response lost")
    exc = PublishError("wrapped api error")  # 无 errcode 属性 = None
    exc.__cause__ = cause
    flag = {"marked": False}
    with pytest.raises(CommitUncertainError):
        with _guard(flag).committing():
            raise exc


def test_wrapped_connect_error_is_clean():
    """外层 errcode=None + __cause__=httpx.ConnectError → clean（连接未建立=未发出，原异常透出）。"""
    cause = httpx.ConnectError("never connected")
    exc = PublishError("wrapped api error")
    exc.__cause__ = cause
    flag = {"marked": False}
    with pytest.raises(PublishError) as ei:
        with _guard(flag).committing():
            raise exc
    # 必须是原异常透出、不是被包成 CommitUncertainError
    assert not isinstance(ei.value, CommitUncertainError)
