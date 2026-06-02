"""Tests for server.app.modules.tasks.runner."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from server.app.modules.tasks.drivers.base import PublishPayload, PublishResult
from server.app.modules.tasks.drivers.toutiao import (
    PublishFillResult,
    ToutiaoUserInputRequired,
)

# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


def _make_stub_article(tmp_path: Path) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        title="Test Article",
        cover_asset=object(),  # non-None → passes the cover check
        content_json="",
        plain_text="body text",
        content_html="",
        body_assets=[],
    )


def _make_stub_account() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        state_path="browser_states/testplat/k1/storage_state.json",
        display_name="Test Account",
    )


def _make_stub_session() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id="sess1",
        display=":99",
        novnc_url="http://localhost:6080",
        browser_context=None,  # None → triggers first-launch path in run_publish
    )


def _make_stub_payload(tmp_path: Path) -> PublishPayload:
    return PublishPayload(
        title="Test Article",
        cover_asset_path=tmp_path / "cover.jpg",
        body_segments=[],
        account_key="k1",
        state_path=tmp_path / "browser_states/testplat/k1/storage_state.json",
        display_name="Test Account",
        platform_code="testplat",
    )


def _make_stub_pw_context_page():
    """Return (pw, context, page) stubs."""
    page = types.SimpleNamespace(on=lambda *args, **kwargs: None)
    context = types.SimpleNamespace(
        set_default_navigation_timeout=lambda ms: None,
        new_page=lambda: page,
        close=lambda: None,
    )
    chromium = types.SimpleNamespace(launch_persistent_context=lambda user_data_dir, **kw: context)
    pw = types.SimpleNamespace(
        chromium=chromium,
        stop=lambda: None,
    )
    # sync_playwright() returns a context manager, so we simulate .start()
    pw_cm = types.SimpleNamespace(start=lambda: pw)
    return pw_cm, context, page


# ---------------------------------------------------------------------------
# Shared monkeypatching helper
# ---------------------------------------------------------------------------


def _patch_common(monkeypatch, tmp_path: Path, stub_session, pw_cm, context, page):
    """Apply all patches common to both tests."""
    # Create the state file so the existence check passes
    state_rel = "browser_states/testplat/k1/storage_state.json"
    state_file = tmp_path / state_rel
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("{}")

    stub_payload = _make_stub_payload(tmp_path)

    # get_data_dir → tmp_path
    monkeypatch.setattr(
        "server.app.modules.tasks.runner.get_data_dir",
        lambda: tmp_path,
    )

    # account_key_from_state_path → ("testplat", "k1")
    monkeypatch.setattr(
        "server.app.modules.tasks.runner.account_key_from_state_path",
        lambda state_path: ("testplat", "k1"),
    )

    # _build_payload → returns a stub PublishPayload without touching ORM
    monkeypatch.setattr(
        "server.app.modules.tasks.runner._build_payload",
        lambda article, account, account_key, platform_code, state_path: stub_payload,
    )

    # profile_dir_for_key → a path that doesn't need to exist
    monkeypatch.setattr(
        "server.app.modules.tasks.runner.profile_key_from_state_path",
        lambda state_path: "browser_states/testplat/k1",
    )

    monkeypatch.setattr(
        "server.app.modules.tasks.runner.profile_dir_from_state_path",
        lambda state_path: tmp_path / "profile",
    )

    # get_or_create_account_session → returns stub_session directly
    monkeypatch.setattr(
        "server.app.modules.tasks.runner.get_or_create_account_session",
        lambda platform_code, account_key, profile_key=None: stub_session,
    )

    # stop_remote_browser_session → no-op (called on launch failure path)
    monkeypatch.setattr(
        "server.app.modules.tasks.runner.stop_remote_browser_session",
        lambda session_id: None,
    )

    # sync_playwright → pw_cm
    monkeypatch.setattr(
        "server.app.modules.tasks.runner.sync_playwright",
        lambda: pw_cm,
    )

    # launch_options → minimal dict so options["env"] assignment works
    monkeypatch.setattr(
        "server.app.modules.tasks.runner.launch_options",
        lambda channel, executable_path: {},
    )

    # attach_browser_handles → no-op
    monkeypatch.setattr(
        "server.app.modules.tasks.runner.attach_browser_handles",
        lambda *args, **kwargs: None,
    )


# ---------------------------------------------------------------------------
# Test 1
# ---------------------------------------------------------------------------


def test_run_publish_routes_by_platform_code(monkeypatch, tmp_path):
    """run_publish calls the driver matched by the platform code in state_path."""
    from server.app.modules.tasks import runner as publish_runner

    stub_session = _make_stub_session()
    pw_cm, context, page = _make_stub_pw_context_page()

    _patch_common(monkeypatch, tmp_path, stub_session, pw_cm, context, page)

    publish_called = []
    expected_result = PublishFillResult(
        url="https://example.com/article/1",
        title="Test Article",
        message="发布成功",
    )

    class _StubDriver:
        code = "testplat"
        name = "Test Platform"
        home_url = "https://example.com"
        publish_url = "https://example.com/publish"

        def detect_logged_in(self, *, url, title, body):
            return True

        def publish(self, *, page, context, payload, stop_before_publish):
            publish_called.append(True)
            return expected_result

    stub_driver = _StubDriver()

    monkeypatch.setattr(
        "server.app.modules.tasks.runner.get_driver",
        lambda platform_code: stub_driver,
    )

    article = _make_stub_article(tmp_path)
    account = _make_stub_account()

    result = publish_runner.run_publish(article=article, account=account)

    assert publish_called, "driver.publish was not called"
    assert result == expected_result


# ---------------------------------------------------------------------------
# Test 2
# ---------------------------------------------------------------------------


def test_run_publish_keeps_session_on_user_input_required(monkeypatch, tmp_path):
    """When driver.publish raises ToutiaoUserInputRequired, session is kept alive and exception has session_id/novnc_url."""
    from server.app.modules.tasks import runner as publish_runner

    stub_session = _make_stub_session()
    pw_cm, context, page = _make_stub_pw_context_page()

    _patch_common(monkeypatch, tmp_path, stub_session, pw_cm, context, page)

    class _StubDriver:
        code = "testplat"
        name = "Test Platform"
        home_url = "https://example.com"
        publish_url = "https://example.com/publish"

        def detect_logged_in(self, *, url, title, body):
            return True

        def publish(self, *, page, context, payload, stop_before_publish):
            raise ToutiaoUserInputRequired("needs login")

    stub_driver = _StubDriver()

    monkeypatch.setattr(
        "server.app.modules.tasks.runner.get_driver",
        lambda platform_code: stub_driver,
    )

    kept_alive = []

    monkeypatch.setattr(
        "server.app.modules.tasks.runner.keep_session_alive",
        lambda session_id: kept_alive.append(session_id),
    )

    article = _make_stub_article(tmp_path)
    account = _make_stub_account()

    with pytest.raises(ToutiaoUserInputRequired) as exc_info:
        publish_runner.run_publish(article=article, account=account)

    exc = exc_info.value
    assert kept_alive == [stub_session.id], (
        f"keep_session_alive was not called with '{stub_session.id}'; calls: {kept_alive}"
    )
    assert exc.session_id == stub_session.id, (
        f"Expected session_id={stub_session.id!r}, got {exc.session_id!r}"
    )
    assert exc.novnc_url == stub_session.novnc_url, (
        f"Expected novnc_url={stub_session.novnc_url!r}, got {exc.novnc_url!r}"
    )


def test_run_publish_stops_session_after_auto_publish(monkeypatch, tmp_path):
    from server.app.modules.tasks import runner as publish_runner

    stub_session = _make_stub_session()
    pw_cm, context, page = _make_stub_pw_context_page()

    _patch_common(monkeypatch, tmp_path, stub_session, pw_cm, context, page)

    class _StubDriver:
        code = "testplat"
        name = "Test Platform"
        home_url = "https://example.com"
        publish_url = "https://example.com/publish"

        def detect_logged_in(self, *, url, title, body):
            return True

        def publish(self, *, page, context, payload, stop_before_publish):
            return PublishResult(
                url="https://example.com/article/1", title=payload.title, message="ok"
            )

    stopped = []
    kept_alive = []
    monkeypatch.setattr(
        "server.app.modules.tasks.runner.get_driver", lambda platform_code: _StubDriver()
    )
    monkeypatch.setattr(
        "server.app.modules.tasks.runner.stop_remote_browser_session",
        lambda session_id: stopped.append(session_id),
    )
    monkeypatch.setattr(
        "server.app.modules.tasks.runner.keep_session_alive",
        lambda session_id: kept_alive.append(session_id),
    )

    result = publish_runner.run_publish(
        article=_make_stub_article(tmp_path), account=_make_stub_account()
    )

    assert result.url == "https://example.com/article/1"
    assert stopped == [stub_session.id]
    assert kept_alive == []


def test_run_publish_keeps_session_for_manual_publish(monkeypatch, tmp_path):
    from server.app.modules.tasks import runner as publish_runner

    stub_session = _make_stub_session()
    pw_cm, context, page = _make_stub_pw_context_page()

    _patch_common(monkeypatch, tmp_path, stub_session, pw_cm, context, page)

    class _StubDriver:
        code = "testplat"
        name = "Test Platform"
        home_url = "https://example.com"
        publish_url = "https://example.com/publish"

        def detect_logged_in(self, *, url, title, body):
            return True

        def publish(self, *, page, context, payload, stop_before_publish):
            return PublishResult(url=None, title=payload.title, message="waiting")

    stopped = []
    kept_alive = []
    monkeypatch.setattr(
        "server.app.modules.tasks.runner.get_driver", lambda platform_code: _StubDriver()
    )
    monkeypatch.setattr(
        "server.app.modules.tasks.runner.stop_remote_browser_session",
        lambda session_id: stopped.append(session_id),
    )
    monkeypatch.setattr(
        "server.app.modules.tasks.runner.keep_session_alive",
        lambda session_id: kept_alive.append(session_id),
    )

    result = publish_runner.run_publish(
        article=_make_stub_article(tmp_path),
        account=_make_stub_account(),
        stop_before_publish=True,
    )

    assert result.message == "waiting"
    assert kept_alive == [stub_session.id]
    assert stopped == []


def test_build_payload_resolves_stock_image_segments(monkeypatch, tmp_path):
    from server.app.modules.tasks import runner as publish_runner

    cover_path = tmp_path / "cover.jpg"
    cover_path.write_bytes(b"cover")
    stock_path = tmp_path / "stock.jpg"
    stock_path.write_bytes(b"stock")

    article = types.SimpleNamespace(
        title="Stock image article",
        cover_asset=object(),
        content_json='{"type":"doc","content":[{"type":"image","attrs":{"stockImageId":42,"src":"/api/stock-images/42/file"}}]}',
        plain_text="",
        content_html="",
        body_assets=[],
    )
    account = _make_stub_account()

    monkeypatch.setattr(
        "server.app.modules.tasks.runner.resolve_asset_path", lambda asset: cover_path
    )
    monkeypatch.setattr(
        "server.app.modules.tasks.runner._resolve_stock_image_path",
        lambda stock_image_id: stock_path,
    )

    payload = publish_runner._build_payload(
        article,
        account,
        "k1",
        "testplat",
        tmp_path / "browser_states/testplat/k1/storage_state.json",
    )

    assert payload.body_segments[0].stock_image_id == 42
    assert payload.body_segments[0].image_path == stock_path
    assert payload.temp_files == (stock_path,)
