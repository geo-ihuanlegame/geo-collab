"""I1：头条页内驱动（inpage）终点提交守卫测试（无 DB）。

save=1 真实发布的 page.evaluate(...) 处断网应被 commit_guard 包成 CommitUncertainError，
且进守卫前 mark_pending 触发一次。save=0（草稿/stop_before_publish）不包守卫。
"""

import pytest

from server.app.modules.articles.parser import BodySegment
from server.app.modules.tasks.drivers.base import (
    CommitGuard,
    CommitUncertainError,
    PublishPayload,
)
from server.app.modules.tasks.drivers.toutiao_inpage import ToutiaoInPageDriver
from server.app.shared.resilience import RetryPolicy


class _FakePage:
    """最小桩：goto 成功；evaluate 在真实发布（save=1）调用处抛网络超时（提交边界断网）。

    _wait_editor_ready 只用 get_by_role(...).count() > 0 判断编辑器就绪——这里恒返回 1。
    """

    def __init__(self, *, evaluate_raises):
        self.url = "https://mp.toutiao.com/profile_v4/graphic/publish"
        self._evaluate_raises = evaluate_raises
        self.goto_calls = 0
        self.evaluate_calls = 0

    def goto(self, url, **_kwargs):
        self.goto_calls += 1

    def evaluate(self, _js, _arg):
        self.evaluate_calls += 1
        if self._evaluate_raises is not None:
            raise self._evaluate_raises
        # 草稿路径走到这里：返回一个最小成功信封
        return {
            "ok": True,
            "step": "publish",
            "uploads": [],
            "publish": {"httpStatus": 200, "data": {"code": 0, "data": {"pgc_id": "1"}}},
        }

    class _Locator:
        def count(self):
            return 1

    def get_by_role(self, _role, name=None):
        return _FakePage._Locator()

    def wait_for_timeout(self, _ms):
        pass


def _payload(tmp_path):
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 200)  # 最小 JPEG 头占位
    return PublishPayload(
        title="标题",
        cover_asset_path=cover,
        body_segments=[BodySegment(kind="text", text="正文")],
        account_key="acc",
        state_path=cover,  # 驱动不读它
        display_name="acc",
        platform_code="toutiao",
    )


def test_publish_save1_network_loss_is_uncertain(tmp_path, monkeypatch):
    """save=1 终点断网 → CommitUncertainError，mark_pending 触发一次。"""
    # 让图片瘦身/读取直通，避免依赖 Pillow 细节
    monkeypatch.setattr(
        "server.app.modules.tasks.drivers.toutiao_inpage._b64_of", lambda p: ("AA==", "image/jpeg")
    )

    marked = {"n": 0}
    guard = CommitGuard(mark_pending=lambda: marked.__setitem__("n", marked["n"] + 1))
    # readtimeout 风格的网络异常（httpx.ReadTimeout 模拟提交边界断网）；用裸 TimeoutError 也走「未知」分支
    page = _FakePage(evaluate_raises=TimeoutError("network lost during publish evaluate"))

    with pytest.raises(CommitUncertainError):
        ToutiaoInPageDriver().publish(
            page=page,
            context=None,
            payload=_payload(tmp_path),
            stop_before_publish=False,
            commit_guard=guard,
            retry_policy=RetryPolicy(enabled=False),  # 关重试，专测守卫
        )
    assert marked["n"] == 1  # 进守卫前标记一次
    assert page.evaluate_calls == 1


def test_draft_save0_not_wrapped(tmp_path, monkeypatch):
    """save=0（stop_before_publish）不包守卫：mark_pending 不触发，正常返回草稿结果。"""
    monkeypatch.setattr(
        "server.app.modules.tasks.drivers.toutiao_inpage._b64_of", lambda p: ("AA==", "image/jpeg")
    )

    marked = {"n": 0}
    guard = CommitGuard(mark_pending=lambda: marked.__setitem__("n", marked["n"] + 1))
    page = _FakePage(evaluate_raises=None)

    result = ToutiaoInPageDriver().publish(
        page=page,
        context=None,
        payload=_payload(tmp_path),
        stop_before_publish=True,
        commit_guard=guard,
        retry_policy=RetryPolicy(enabled=False),
    )
    assert marked["n"] == 0  # 草稿路径不进守卫
    assert "草稿" in result.message
