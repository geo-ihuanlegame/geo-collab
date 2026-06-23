# server/tests/test_wechat_mp_retry.py
import httpx
import pytest

from server.app.modules.articles.parser import BodySegment
from server.app.modules.tasks.drivers.base import (
    NOOP_COMMIT_GUARD,
    ApiPublishPayload,
    CommitGuard,
    CommitUncertainError,
    PublishError,
)
from server.app.modules.tasks.drivers.wechat_mp import WeChatMpDriver
from server.app.shared.resilience import RetryPolicy


def _payload(tmp_path):
    img = tmp_path / "c.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 200)  # 最小 JPEG 头占位
    return ApiPublishPayload(
        title="t",
        body_segments=[BodySegment(kind="text", text="hi")],
        cover_path=img,
        display_name="acc",
        platform_code="wechat_mp",
        access_token="tok",
    )


def _ok(json_body):
    return httpx.Response(200, json=json_body)


def test_upload_retries_then_succeeds(tmp_path, monkeypatch):
    # 让图片压缩直通，避免依赖 Pillow 细节
    monkeypatch.setattr(
        "server.app.modules.tasks.drivers.wechat_mp.compress_cover_to_jpeg", lambda b: b
    )
    calls = {"thumb": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "add_material" in url:
            calls["thumb"] += 1
            if calls["thumb"] < 3:
                raise httpx.ReadTimeout("blip", request=request)
            return _ok({"media_id": "m1"})
        if "draft/add" in url:
            return _ok({"media_id": "draft1"})
        return _ok({})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    driver = WeChatMpDriver()
    result = driver.publish_api(
        payload=_payload(tmp_path),
        client=client,
        commit_guard=NOOP_COMMIT_GUARD,
        retry_policy=RetryPolicy(max_attempts=3, base_delay=0.0, jitter=0.0, max_elapsed=None),
    )
    assert "draft1" in result.message
    assert calls["thumb"] == 3


def test_add_draft_network_loss_is_commit_uncertain(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "server.app.modules.tasks.drivers.wechat_mp.compress_cover_to_jpeg", lambda b: b
    )
    marked = {"n": 0}
    draft_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "add_material" in url:
            return _ok({"media_id": "m1"})
        if "draft/add" in url:
            draft_calls["n"] += 1
            raise httpx.ReadTimeout("response lost", request=request)
        return _ok({})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    guard = CommitGuard(mark_pending=lambda: marked.__setitem__("n", marked["n"] + 1))
    driver = WeChatMpDriver()
    with pytest.raises(CommitUncertainError):
        driver.publish_api(
            payload=_payload(tmp_path),
            client=client,
            commit_guard=guard,
            retry_policy=RetryPolicy(max_attempts=3, base_delay=0.0, jitter=0.0, max_elapsed=None),
        )
    assert marked["n"] == 1  # 进守卫前标记一次
    assert draft_calls["n"] == 1  # add_draft 未重试


def test_business_errcode_stays_publish_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "server.app.modules.tasks.drivers.wechat_mp.compress_cover_to_jpeg", lambda b: b
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "add_material" in url:
            return _ok({"media_id": "m1"})
        if "draft/add" in url:
            return _ok({"errcode": 40164, "errmsg": "ip not in whitelist"})
        return _ok({})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    driver = WeChatMpDriver()
    with pytest.raises(PublishError) as exc_info:
        driver.publish_api(
            payload=_payload(tmp_path),
            client=client,
            commit_guard=CommitGuard(mark_pending=lambda: None),
            retry_policy=RetryPolicy(max_attempts=3, base_delay=0.0, jitter=0.0, max_elapsed=None),
        )
    assert not isinstance(exc_info.value, CommitUncertainError)  # 业务错误码不应判为 uncertain
