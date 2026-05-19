"""Tests for PlatformDriver registry and ToutiaoDriver implementation."""
import pytest


def test_toutiao_driver_registered():
    import server.app.modules.tasks.drivers.toutiao  # triggers register()
    from server.app.modules.tasks.drivers import all_driver_codes, get_driver

    assert "toutiao" in all_driver_codes()
    driver = get_driver("toutiao")
    assert driver.code == "toutiao"
    assert driver.name == "头条号"
    assert driver.home_url.startswith("https://mp.toutiao.com")


def test_get_unknown_driver_raises():
    from server.app.modules.tasks.drivers import get_driver

    with pytest.raises(ValueError, match="Unknown platform"):
        get_driver("nonexistent_platform_xyz")


def test_detect_logged_in_returns_true_for_profile_page():
    from server.app.modules.tasks.drivers.toutiao import ToutiaoDriver

    d = ToutiaoDriver()
    assert d.detect_logged_in(
        url="https://mp.toutiao.com/profile_v4/index",
        title="头条号",
        body="",
    ) is True


def test_detect_logged_in_returns_false_for_login_page():
    from server.app.modules.tasks.drivers.toutiao import ToutiaoDriver

    d = ToutiaoDriver()
    assert d.detect_logged_in(
        url="https://sso.toutiao.com/login",
        title="登录",
        body="扫码登录",
    ) is False


def test_detect_logged_in_returns_false_for_captcha():
    from server.app.modules.tasks.drivers.toutiao import ToutiaoDriver

    d = ToutiaoDriver()
    assert d.detect_logged_in(
        url="https://mp.toutiao.com",
        title="安全验证",
        body="验证码",
    ) is False


def test_driver_registry_rejects_duplicate():
    from server.app.modules.tasks.drivers import _REGISTRY, register
    from server.app.modules.tasks.drivers.toutiao import ToutiaoDriver

    import server.app.modules.tasks.drivers.toutiao  # ensure toutiao is already registered

    with pytest.raises(ValueError, match="already registered"):
        register(ToutiaoDriver())


def test_platform_driver_protocol_isinstance():
    import server.app.modules.tasks.drivers.toutiao
    from server.app.modules.tasks.drivers import PlatformDriver, get_driver

    driver = get_driver("toutiao")
    assert isinstance(driver, PlatformDriver)


def test_fill_title_uses_fill_not_press_sequentially():
    """确保 _fill_title 用 fill() 而非 press_sequentially()，避免 IME 吞字。"""
    from unittest.mock import MagicMock, call

    from server.app.modules.tasks.drivers.toutiao import _fill_title

    field = MagicMock()
    page = MagicMock()
    page.get_by_role.return_value = field
    field.wait_for.return_value = None

    _fill_title(page, "告别踩雷下载2026口碑长线运营游戏谁是长青代表")

    field.fill.assert_called_once()
    assert not field.press_sequentially.called, "press_sequentially should not be called — it triggers IME bugs"


def test_insert_body_text_uses_clipboard_not_keyboard_type():
    """确保 _insert_body_text 用剪贴板粘贴而非 keyboard.type()，避免 IME 吞字。"""
    from unittest.mock import MagicMock

    from server.app.modules.tasks.drivers.toutiao import _insert_body_text

    page = MagicMock()

    _insert_body_text(page, "告别踩雷下载2026口碑长线运营游戏谁是长青代表")

    page.evaluate.assert_called_once()
    page.keyboard.press.assert_called_once_with("Control+v")
    assert not page.keyboard.type.called, "keyboard.type should not be called — it triggers IME bugs"


def _slot_marker(slot):
    if isinstance(slot, dict):
        return slot["marker"]
    return slot.marker


def _slot_asset_id(slot):
    if isinstance(slot, dict):
        if "asset_id" in slot:
            return slot["asset_id"]
        if "image_asset_id" in slot:
            return slot["image_asset_id"]
        return slot["segment"].image_asset_id
    if hasattr(slot, "asset_id"):
        return slot.asset_id
    if hasattr(slot, "image_asset_id"):
        return slot.image_asset_id
    return slot.segment.image_asset_id


def test_build_body_fill_plan_preserves_interleaved_text_image_order():
    from pathlib import Path

    from server.app.modules.articles.tiptap_Parser import BodySegment
    from server.app.modules.tasks.drivers.toutiao import _build_body_fill_plan

    plan = _build_body_fill_plan(
        [
            BodySegment(kind="text", text="before image"),
            BodySegment(kind="image", image_path=Path("first.png"), image_asset_id="asset-1"),
            BodySegment(kind="text", text="between images"),
            BodySegment(kind="image", image_path=Path("second.png"), image_asset_id="asset-2"),
        ]
    )

    markers = [_slot_marker(slot) for slot in plan.image_slots]

    assert [text in plan.full_text for text in ("before image", "between images")] == [True, True]
    assert len(markers) == 2
    assert markers[0] != markers[1]
    assert plan.full_text.index("before image") < plan.full_text.index(markers[0])
    assert plan.full_text.index(markers[0]) < plan.full_text.index("between images")
    assert plan.full_text.index("between images") < plan.full_text.index(markers[1])


def test_build_body_fill_plan_keeps_duplicate_asset_ids_as_distinct_slots():
    from pathlib import Path

    from server.app.modules.articles.tiptap_Parser import BodySegment
    from server.app.modules.tasks.drivers.toutiao import _build_body_fill_plan

    plan = _build_body_fill_plan(
        [
            BodySegment(kind="image", image_path=Path("first.png"), image_asset_id="repeat-asset"),
            BodySegment(kind="image", image_path=Path("second.png"), image_asset_id="repeat-asset"),
        ]
    )

    markers = [_slot_marker(slot) for slot in plan.image_slots]

    assert [_slot_asset_id(slot) for slot in plan.image_slots] == ["repeat-asset", "repeat-asset"]
    assert len(markers) == 2
    assert len(set(markers)) == 2
    assert [plan.full_text.count(marker) for marker in markers] == [1, 1]
    assert plan.full_text.index(markers[0]) < plan.full_text.index(markers[1])
