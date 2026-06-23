import pytest

from server.app.modules.tasks.drivers.base import CommitGuard, CommitUncertainError
from server.app.modules.tasks.drivers.toutiao import _click_publish_and_wait


class _Btn:
    def __init__(self, page, fail=False):
        self._page = page
        self._fail = fail

    def wait_for(self, **_):
        pass

    def click(self):
        if self._fail:
            raise TimeoutError("network lost during confirm click")  # 模拟点确认发布时断网
        self._page.clicked.append("preview")


class _FakePage:
    """最小桩：「预览并发布」点击成功；「确认发布」点击抛网络超时（提交边界处断网）。"""

    def __init__(self):
        self.url = "https://mp.toutiao.com/profile_v4/graphic/publish"
        self.clicked = []

    def get_by_role(self, role, name=None):
        return _Btn(self, fail=(name == "确认发布"))

    def wait_for_timeout(self, _ms):
        pass


def test_confirm_click_network_loss_is_uncertain(monkeypatch):
    # 关闭弹窗 / 截图 / 正文摘要等噪声（确认发布失败分支会调用）
    monkeypatch.setattr(
        "server.app.modules.tasks.drivers.toutiao._dismiss_blocking_popups", lambda p: None
    )
    monkeypatch.setattr("server.app.modules.tasks.drivers.toutiao._screenshot", lambda p: None)
    monkeypatch.setattr("server.app.modules.tasks.drivers.toutiao._body_text_hint", lambda p: "")

    marked = {"n": 0}
    guard = CommitGuard(mark_pending=lambda: marked.__setitem__("n", marked["n"] + 1))
    page = _FakePage()
    with pytest.raises(CommitUncertainError):
        _click_publish_and_wait(page, stop_before_publish=False, commit_guard=guard)
    assert marked["n"] == 1  # 进守卫前标记一次
