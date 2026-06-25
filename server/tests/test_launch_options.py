"""无 DB 单元测试：accounts.service.launch_options headless 形参契约。"""

from server.app.modules.accounts.service import launch_options


def test_launch_options_default_headed() -> None:
    """默认不传 headless → headed（登录路径恒为 headed）。"""
    opts = launch_options("chromium", None)
    assert opts["headless"] is False


def test_launch_options_headless_true() -> None:
    """显式传 headless=True → headless（发布路径 opt-in）。"""
    opts = launch_options("chromium", None, headless=True)
    assert opts["headless"] is True


def test_launch_options_headless_false() -> None:
    """显式传 headless=False → headed。"""
    opts = launch_options("chromium", None, headless=False)
    assert opts["headless"] is False
