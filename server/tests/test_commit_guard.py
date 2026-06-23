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
