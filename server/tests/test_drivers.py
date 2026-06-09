"""测试 PlatformDriver 注册表和 ToutiaoDriver 实现。"""

import pytest


def test_toutiao_driver_registered():
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
    assert (
        d.detect_logged_in(
            url="https://mp.toutiao.com/profile_v4/index",
            title="头条号",
            body="",
        )
        is True
    )


def test_detect_logged_in_returns_false_for_login_page():
    from server.app.modules.tasks.drivers.toutiao import ToutiaoDriver

    d = ToutiaoDriver()
    assert (
        d.detect_logged_in(
            url="https://sso.toutiao.com/login",
            title="登录",
            body="扫码登录",
        )
        is False
    )


def test_detect_logged_in_returns_false_for_captcha():
    from server.app.modules.tasks.drivers.toutiao import ToutiaoDriver

    d = ToutiaoDriver()
    assert (
        d.detect_logged_in(
            url="https://mp.toutiao.com",
            title="安全验证",
            body="验证码",
        )
        is False
    )


def test_driver_registry_rejects_duplicate():
    from server.app.modules.tasks.drivers import register
    from server.app.modules.tasks.drivers.toutiao import ToutiaoDriver

    with pytest.raises(ValueError, match="already registered"):
        register(ToutiaoDriver())


def test_platform_driver_protocol_isinstance():
    from server.app.modules.tasks.drivers import PlatformDriver, get_driver

    driver = get_driver("toutiao")
    assert isinstance(driver, PlatformDriver)


def test_fill_title_uses_fill_not_press_sequentially():
    """确保 _fill_title 用 fill() 而非 press_sequentially()，避免 IME 吞字。"""
    from unittest.mock import MagicMock

    from server.app.modules.tasks.drivers.toutiao import _fill_title

    field = MagicMock()
    page = MagicMock()
    page.get_by_role.return_value = field
    field.wait_for.return_value = None

    _fill_title(page, "告别踩雷下载2026口碑长线运营游戏谁是长青代表")

    field.fill.assert_called_once()
    assert not field.press_sequentially.called, (
        "press_sequentially should not be called — it triggers IME bugs"
    )
