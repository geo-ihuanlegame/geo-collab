"""Unit tests for the async login-browser broker.

These run WITHOUT a database or a real browser: the broker's Playwright calls
go through monkeypatchable module seams (``_pw_open`` / ``_pw_read`` /
``_pw_close``), so the concurrency / cap / timeout behaviour can be verified on
any machine. The whole point of the broker is that multiple kept-alive login
browsers share ONE asyncio loop, which is exactly what async Playwright is built
for — so two concurrent logins must both succeed (the regression this fixes).
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("GEO_JWT_SECRET", "test-secret-for-login-broker-unit-tests")
os.environ.setdefault("GEO_DATA_DIR", tempfile.mkdtemp(prefix="geo-broker-test-"))

import pytest

from server.app.modules.accounts import login_broker as lb
from server.app.shared.errors import ClientError


class _FakeLocator:
    async def inner_text(self, **_kw):
        return "BODY"


class _FakePage:
    def __init__(self, url="https://example.com"):
        self.url = url
        self.closed = False
        self.goto_calls: list[str] = []

    async def goto(self, url, **_kw):
        self.goto_calls.append(url)
        self.url = url

    async def wait_for_load_state(self, *_a, **_k):
        return None

    async def title(self):
        return "TITLE"

    def locator(self, _sel):
        return _FakeLocator()

    async def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, page):
        self._pages = [page]
        self.closed = False
        self.saved_path = None

    @property
    def pages(self):
        return list(self._pages)

    async def storage_state(self, path=None):
        self.saved_path = path
        if path:
            Path(path).write_text("{}", encoding="utf-8")

    async def close(self):
        self.closed = True


class _FakePw:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


def _fake_open_factory(delay=0.0):
    async def _fake_open(profile_dir, options, display):
        if delay:
            import asyncio

            await asyncio.sleep(delay)
        page = _FakePage()
        return _FakePw(), _FakeContext(page), page

    return _fake_open


@pytest.fixture
def broker(monkeypatch):
    monkeypatch.setattr(lb, "_pw_open", _fake_open_factory())
    b = lb.LoginBrowserBroker()
    try:
        yield b
    finally:
        b.shutdown()


def test_two_concurrent_logins_both_succeed(monkeypatch):
    """Two kept-alive login browsers started concurrently must BOTH succeed.

    This is the regression: the old single-thread sync design threw
    "Playwright Sync API inside the asyncio loop" on the second one.
    """
    # both launches are in-flight at the same time (50ms open each)
    monkeypatch.setattr(lb, "_pw_open", _fake_open_factory(delay=0.05))
    b = lb.LoginBrowserBroker()
    try:
        errors: dict[str, BaseException] = {}

        def _do(sid):
            try:
                b.launch_login_browser(sid, profile_dir=Path("p"), options={}, display=":1")
            except BaseException as exc:  # noqa: BLE001 - record for assertion
                errors[sid] = exc

        t1 = threading.Thread(target=_do, args=("s1",))
        t2 = threading.Thread(target=_do, args=("s2",))
        start = time.monotonic()
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        elapsed = time.monotonic() - start

        assert errors == {}, f"concurrent launches failed: {errors}"
        assert b.owns("s1") and b.owns("s2")
        # truly concurrent on one loop: ~0.05s, not serialized ~0.10s
        assert elapsed < 0.09, f"launches were serialized ({elapsed:.3f}s)"
    finally:
        b.shutdown()


def test_cap_rejects_overflow(monkeypatch):
    """The (cap+1)th concurrent login is rejected with a clear error, not launched."""
    from server.app.core.config import get_settings

    monkeypatch.setattr(lb, "_pw_open", _fake_open_factory())
    monkeypatch.setenv("GEO_LOGIN_MAX_CONCURRENT_BROWSERS", "2")
    get_settings.cache_clear()
    b = lb.LoginBrowserBroker()
    try:
        b.launch_login_browser("a", profile_dir=Path("p"), options={}, display=":1")
        b.launch_login_browser("b", profile_dir=Path("p"), options={}, display=":2")
        with pytest.raises(ClientError):
            b.launch_login_browser("c", profile_dir=Path("p"), options={}, display=":3")
        assert b.active_count() == 2
        assert not b.owns("c")
    finally:
        get_settings.cache_clear()
        b.shutdown()


def test_launch_timeout_cancels_and_loop_survives(monkeypatch):
    """A hung launch times out (cancelled), does not leak a session, loop stays usable."""
    import asyncio
    from concurrent.futures import TimeoutError as FuturesTimeoutError

    async def _slow_open(profile_dir, options, display):
        await asyncio.sleep(5)

    monkeypatch.setattr(lb, "_pw_open", _slow_open)
    monkeypatch.setattr(lb, "LAUNCH_TIMEOUT_SECONDS", 0.1)
    b = lb.LoginBrowserBroker()
    try:
        with pytest.raises(FuturesTimeoutError):
            b.launch_login_browser("slow", profile_dir=Path("p"), options={}, display=":1")
        assert not b.owns("slow")
        # loop survives: a subsequent normal launch still works
        monkeypatch.setattr(lb, "_pw_open", _fake_open_factory())
        b.launch_login_browser("ok", profile_dir=Path("p"), options={}, display=":2")
        assert b.owns("ok")
    finally:
        b.shutdown()


def test_read_login_state_detects_and_saves(broker, tmp_path):
    broker.launch_login_browser("s", profile_dir=Path("p"), options={}, display=":1")
    state_path = tmp_path / "storage_state.json"
    seen: dict[str, str] = {}

    def _detect(url, title, body):
        seen.update(url=url, title=title, body=body)
        return True

    result = broker.read_login_state("s", detect=_detect, state_path=state_path)
    assert result.logged_in is True
    assert result.url == "https://example.com"
    assert result.title == "TITLE"
    assert seen["body"] == "BODY"
    assert state_path.exists()


def test_close_is_idempotent_and_frees_slot(broker):
    broker.launch_login_browser("s", profile_dir=Path("p"), options={}, display=":1")
    assert broker.owns("s")
    broker.close("s")
    assert not broker.owns("s")
    broker.close("s")  # idempotent — second close must not raise
    broker.close_if_owned("never-existed")  # unknown id — no-op


def test_close_if_owned_does_not_start_loop():
    """Tearing down a non-login (e.g. publish) session must not spin up the broker loop."""
    b = lb.LoginBrowserBroker()
    b.close_if_owned("some-publish-session")
    assert b._thread is None
