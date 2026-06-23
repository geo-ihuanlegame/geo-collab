"""server.app.modules.tasks.runner 的测试。"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from server.app.modules.tasks.drivers.base import PublishError, PublishPayload, PublishResult
from server.app.modules.tasks.drivers.toutiao import (
    PublishFillResult,
    ToutiaoUserInputRequired,
)

# ---------------------------------------------------------------------------
# 辅助函数 / 桩对象
# ---------------------------------------------------------------------------


def _make_stub_article(tmp_path: Path) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        title="Test Article",
        cover_asset=object(),  # 非 None → 通过封面检查
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
        browser_context=None,  # None → 触发 run_publish 的首次启动分支
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
    """返回 (pw, context, page) 桩对象。"""
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
    # sync_playwright() 返回上下文管理器，这里模拟 .start()。
    pw_cm = types.SimpleNamespace(start=lambda: pw)
    return pw_cm, context, page


# ---------------------------------------------------------------------------
# 共享 monkeypatch 辅助函数
# ---------------------------------------------------------------------------


def _patch_common(monkeypatch, tmp_path: Path, stub_session, pw_cm, context, page):
    """应用两个测试共用的全部 patch。"""
    # 创建 state 文件，让存在性检查通过。
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

    # _build_payload → 返回桩 PublishPayload，不触碰 ORM
    monkeypatch.setattr(
        "server.app.modules.tasks.runner._build_payload",
        lambda article, account, account_key, platform_code, state_path: stub_payload,
    )

    # profile_dir_for_key → 一个无需真实存在的路径
    monkeypatch.setattr(
        "server.app.modules.tasks.runner.profile_key_from_state_path",
        lambda state_path: "browser_states/testplat/k1",
    )

    monkeypatch.setattr(
        "server.app.modules.tasks.runner.profile_dir_from_state_path",
        lambda state_path: tmp_path / "profile",
    )

    # get_or_create_account_session → 直接返回 stub_session
    monkeypatch.setattr(
        "server.app.modules.tasks.runner.get_or_create_account_session",
        lambda platform_code, account_key, profile_key=None: stub_session,
    )

    # stop_remote_browser_session → 空操作（在启动失败分支被调用）
    monkeypatch.setattr(
        "server.app.modules.tasks.runner.stop_remote_browser_session",
        lambda session_id: None,
    )

    # sync_playwright → 返回 pw_cm
    monkeypatch.setattr(
        "server.app.modules.tasks.runner.sync_playwright",
        lambda: pw_cm,
    )

    # launch_options → 返回最小 dict，让 options["env"] 赋值能工作
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
# 测试 1
# ---------------------------------------------------------------------------


def test_run_publish_routes_by_platform_code(monkeypatch, tmp_path):
    """run_publish 会调用 state_path 里平台编码匹配的驱动。"""
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

        def publish(
            self,
            *,
            page,
            context,
            payload,
            stop_before_publish,
            commit_guard=None,
            retry_policy=None,
        ):
            publish_called.append(True)
            return expected_result

    stub_driver = _StubDriver()

    monkeypatch.setattr(
        "server.app.modules.tasks.runner.resolve_driver",
        lambda platform_code: stub_driver,
    )

    article = _make_stub_article(tmp_path)
    account = _make_stub_account()

    result = publish_runner.run_publish(article=article, account=account)

    assert publish_called, "driver.publish was not called"
    assert result == expected_result


# ---------------------------------------------------------------------------
# 测试 2
# ---------------------------------------------------------------------------


def test_run_publish_keeps_session_on_user_input_required(monkeypatch, tmp_path):
    """driver.publish 抛 ToutiaoUserInputRequired 时保留会话，并在异常中带 session_id/novnc_url。"""
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

        def publish(
            self,
            *,
            page,
            context,
            payload,
            stop_before_publish,
            commit_guard=None,
            retry_policy=None,
        ):
            raise ToutiaoUserInputRequired("needs login")

    stub_driver = _StubDriver()

    monkeypatch.setattr(
        "server.app.modules.tasks.runner.resolve_driver",
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

        def publish(
            self,
            *,
            page,
            context,
            payload,
            stop_before_publish,
            commit_guard=None,
            retry_policy=None,
        ):
            return PublishResult(
                url="https://example.com/article/1", title=payload.title, message="ok"
            )

    stopped = []
    kept_alive = []
    monkeypatch.setattr(
        "server.app.modules.tasks.runner.resolve_driver", lambda platform_code: _StubDriver()
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

        def publish(
            self,
            *,
            page,
            context,
            payload,
            stop_before_publish,
            commit_guard=None,
            retry_policy=None,
        ):
            return PublishResult(url=None, title=payload.title, message="waiting")

    stopped = []
    kept_alive = []
    monkeypatch.setattr(
        "server.app.modules.tasks.runner.resolve_driver", lambda platform_code: _StubDriver()
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
        lambda stock_image_id, *, missing_ok=False: stock_path,
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


def test_build_payload_skips_missing_stock_image(monkeypatch, tmp_path):
    """图库图片已被删除时，浏览器驱动 payload 应跳过该图、照常构建，不抛 PublishError（#36）。"""
    from server.app.modules.tasks import runner as publish_runner

    cover_path = tmp_path / "cover.jpg"
    cover_path.write_bytes(b"cover")

    article = types.SimpleNamespace(
        title="Article with dead stock image",
        cover_asset=object(),
        content_json='{"type":"doc","content":[{"type":"image","attrs":{"stockImageId":36,"src":"/api/stock-images/36/file"}},{"type":"paragraph","content":[{"type":"text","text":"正文"}]}]}',
        plain_text="",
        content_html="",
        body_assets=[],
    )
    account = _make_stub_account()

    monkeypatch.setattr(
        "server.app.modules.tasks.runner.resolve_asset_path", lambda asset: cover_path
    )
    # 模拟图库图片已删除：_resolve_stock_image_path 在 missing_ok 下返回 None
    monkeypatch.setattr(
        "server.app.modules.tasks.runner._resolve_stock_image_path",
        lambda stock_image_id, *, missing_ok=False: None,
    )

    payload = publish_runner._build_payload(
        article,
        account,
        "k1",
        "testplat",
        tmp_path / "browser_states/testplat/k1/storage_state.json",
    )

    assert all(seg.kind != "image" for seg in payload.body_segments)
    assert payload.temp_files == ()


def test_build_api_payload_skips_missing_stock_image(monkeypatch, tmp_path):
    """微信公众号（API 驱动）payload 应跳过已删除的图库正文图，不抛 PublishError（#36）。"""
    from server.app.modules.tasks import runner_api

    article = types.SimpleNamespace(
        title="WeChat article with dead stock image",
        cover_asset=None,
        content_json='{"type":"doc","content":[{"type":"image","attrs":{"stockImageId":36,"src":"/api/stock-images/36/file"}},{"type":"paragraph","content":[{"type":"text","text":"正文"}]}]}',
        plain_text="",
        content_html="",
        body_assets=[],
    )
    account = types.SimpleNamespace(display_name="测试公众号")

    monkeypatch.setattr(
        "server.app.modules.tasks.runner._resolve_stock_image_path",
        lambda stock_image_id, *, missing_ok=False: None,
    )

    payload = runner_api._build_api_payload(article, account, "tok", "wechat_mp")

    assert all(seg.kind != "image" for seg in payload.body_segments)
    assert payload.temp_files == ()


def test_resolve_stock_image_path_missing_ok_returns_none(monkeypatch):
    """图库记录缺失时：missing_ok=True 返回 None；默认仍抛 PublishError（保持原契约）。"""
    from server.app.modules.tasks import runner as publish_runner

    class _FakeSession:
        def get(self, model, pk):  # noqa: ARG002
            return None

        def close(self) -> None:
            pass

    monkeypatch.setattr("server.app.db.session.SessionLocal", lambda: _FakeSession())

    assert publish_runner._resolve_stock_image_path(36, missing_ok=True) is None

    with pytest.raises(PublishError, match="图片库图片不存在"):
        publish_runner._resolve_stock_image_path(36)
