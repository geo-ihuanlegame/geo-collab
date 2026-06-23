import httpx
import pytest

from server.app.shared.resilience import RetryPolicy, default_is_transient, retry_call


def test_retries_transient_then_succeeds():
    calls = {"n": 0}
    delays = []

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadTimeout("blip")
        return "ok"

    out = retry_call(
        fn,
        policy=RetryPolicy(max_attempts=3, base_delay=1.0, jitter=0.0),
        sleeper=delays.append,
        monotonic=lambda: 0.0,
    )
    assert out == "ok"
    assert calls["n"] == 3
    assert delays == [1.0, 2.0]  # base, base*multiplier


def test_permanent_not_retried():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ValueError("logic")

    with pytest.raises(ValueError):
        retry_call(fn, policy=RetryPolicy(max_attempts=5), sleeper=lambda d: None)
    assert calls["n"] == 1


def test_exhausts_and_raises_last():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise httpx.ConnectError("down")

    with pytest.raises(httpx.ConnectError):
        retry_call(
            fn,
            policy=RetryPolicy(max_attempts=2, base_delay=0.5, jitter=0.0),
            sleeper=lambda d: None,
            monotonic=lambda: 0.0,
        )
    assert calls["n"] == 2


def test_max_elapsed_cuts_off_before_sleeping():
    clock = {"t": 0.0}
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        clock["t"] += 40.0  # 每次调用推进 40s
        raise httpx.ReadTimeout("slow")

    with pytest.raises(httpx.ReadTimeout):
        retry_call(
            fn,
            policy=RetryPolicy(max_attempts=5, base_delay=1.0, jitter=0.0, max_elapsed=60.0),
            sleeper=lambda d: None,
            monotonic=lambda: clock["t"],
        )
    # attempt1 后 elapsed=40, 40+1<60 继续; attempt2 后 elapsed=80, 80+2>60 截止
    assert calls["n"] == 2


def test_disabled_policy_calls_once():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise httpx.ReadTimeout("x")

    with pytest.raises(httpx.ReadTimeout):
        retry_call(fn, policy=RetryPolicy(enabled=False), sleeper=lambda d: None)
    assert calls["n"] == 1


def test_classifier_httpx_and_playwright():
    assert default_is_transient(httpx.ConnectError("x")) is True
    assert default_is_transient(httpx.ReadTimeout("x")) is True
    assert default_is_transient(ValueError("x")) is False

    class _FakePwTimeout(Exception):
        pass

    _FakePwTimeout.__module__ = "playwright._impl._errors"
    _FakePwTimeout.__name__ = "TimeoutError"
    assert default_is_transient(_FakePwTimeout()) is True

    request = httpx.Request("GET", "http://x")
    err_5xx = httpx.HTTPStatusError(
        "x", request=request, response=httpx.Response(502, request=request)
    )
    assert default_is_transient(err_5xx) is True
    err_4xx = httpx.HTTPStatusError(
        "x", request=request, response=httpx.Response(404, request=request)
    )
    assert default_is_transient(err_4xx) is False


def test_get_publish_retry_policy_reads_settings(monkeypatch):
    from server.app.core.config import get_publish_retry_policy, get_settings

    monkeypatch.setenv("GEO_PUBLISH_RETRY_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("GEO_PUBLISH_RETRY_BASE_DELAY_SECONDS", "2.5")
    get_settings.cache_clear()
    policy = get_publish_retry_policy()
    assert policy.max_attempts == 5
    assert policy.base_delay == 2.5
    get_settings.cache_clear()
