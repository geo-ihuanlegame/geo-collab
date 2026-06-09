"""全局异常 → HTTP 状态码 映射契约（CLAUDE.md 关键不变量）。

service 层必须抛命名异常（ClientError/ConflictError/ValidationError/AccountError），
不能抛裸 ValueError —— 没有针对 ValueError 的全局兜底，它会落到 catch-all → 500。

本测试挂一组 throwaway 路由分别抛各类异常，钉住 main.create_app() 注册的映射：
    ConflictError   → 409
    ValidationError → 400
    AccountError    → 400
    ClientError     → 400
    裸 ValueError   → 500   （即「service 层别这么干」的反面证据）

一旦有人改了处理器注册（状态码/顺序/漏注册），或给 ValueError 加了 400 兜底导致
真·程序错误被当成客户端错误吞掉，本测试报警。
"""

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from server.app.shared.errors import AccountError, ClientError, ConflictError, ValidationError
from server.tests.utils import build_test_app

_PREFIX = "/__test_errmap__"


def _mount_error_routes(app) -> None:
    r = APIRouter()

    @r.get("/conflict")
    def _conflict():
        raise ConflictError("dup")

    @r.get("/validation")
    def _validation():
        raise ValidationError("bad")

    @r.get("/account")
    def _account():
        raise AccountError("acct")

    @r.get("/client")
    def _client():
        raise ClientError("client")

    @r.get("/valueerror")
    def _value():
        raise ValueError("oops")

    # SPA 兜底路由 @app.get("/{full_path:path}") 在 create_app 时已注册，会先于后加的路由匹配
    # （把非 API 路径返回 index.html，导致 200）。故把临时路由插到最前确保先命中。
    before = len(app.router.routes)
    app.include_router(r, prefix=_PREFIX)
    added = app.router.routes[before:]
    del app.router.routes[before:]
    app.router.routes[0:0] = added


@pytest.mark.mysql
def test_named_exceptions_map_to_http_status(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        app = test_app.client.app
        _mount_error_routes(app)
        # raise_server_exceptions=False：让兜底 Exception 处理器产出的 500 响应被返回，
        # 而不是把 ValueError 原样重新抛进测试进程。
        client = TestClient(app, raise_server_exceptions=False)

        assert client.get(f"{_PREFIX}/conflict").status_code == 409
        assert client.get(f"{_PREFIX}/validation").status_code == 400
        assert client.get(f"{_PREFIX}/account").status_code == 400
        assert client.get(f"{_PREFIX}/client").status_code == 400

        # 裸 ValueError 无命名处理器 → 落到兜底 Exception → 500。
        # 这条断言正是要固化「别用裸 ValueError 表达客户端错误」：否则真错被掩成 400 也好、
        # 客户端错误被暴成 500 也好，都会被这里捕捉到。
        ve = client.get(f"{_PREFIX}/valueerror")
        assert ve.status_code == 500, ve.text
        assert ve.json()["detail"] == "服务器内部错误"
    finally:
        test_app.cleanup()
