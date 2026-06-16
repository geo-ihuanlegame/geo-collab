"""登录限流测试（POST /api/auth/login 上的 @limiter.limit("5/minute")）。

此前全仓无 429 断言。限流是登录暴破的唯一防线，值得回归保护。

注意：limiter 是模块级单例 + 内存存储，按客户端 IP（TestClient 恒为 "testclient"）计数，
状态跨请求/跨测试累积。故本测试前后都 reset，既保证自身确定性，也不污染其它调用登录的测试。
"""

from server.app.core.limiter import limiter
from server.tests.utils import build_test_app


def test_login_blocked_with_429_after_exceeding_limit(monkeypatch):
    """同一客户端连续登录：前 5 次按业务返回 401，第 6 次被限流拦下返回 429。"""
    test_app = build_test_app(monkeypatch)
    limiter.reset()
    try:
        statuses = []
        for _ in range(6):
            resp = test_app.client.post(
                "/api/auth/login",
                json={"username": "testadmin", "password": "wrong-password"},
            )
            statuses.append(resp.status_code)

        # 前 5 次走到业务逻辑（密码错 → 401），未被限流
        assert statuses[:5] == [401, 401, 401, 401, 401], statuses
        # 第 6 次越过 5/minute，被 RateLimitExceeded handler 拦下
        assert statuses[5] == 429, statuses
    finally:
        limiter.reset()
        test_app.cleanup()


def test_correct_credentials_also_count_toward_limit(monkeypatch):
    """限流在进入业务逻辑前生效：即便用对的密码，超额后第 6 次仍 429（而非 200）。

    判别性：若限流被错误地放在「仅失败登录」之后，正确凭据就能绕过——本断言即红。
    """
    test_app = build_test_app(monkeypatch)
    limiter.reset()
    try:
        last = None
        for _ in range(6):
            last = test_app.client.post(
                "/api/auth/login",
                json={"username": "testadmin", "password": "testadmin"},
            )
        assert last is not None
        assert last.status_code == 429, last.text
    finally:
        limiter.reset()
        test_app.cleanup()
