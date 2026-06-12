"""wechat_client 纯函数测试：httpx.MockTransport 打桩，无 DB、无网络。"""

import json

import httpx
import pytest

from server.app.modules.tasks.drivers.wechat_client import (
    WeChatApiError,
    add_draft,
    build_draft_article,
    fetch_access_token,
    upload_content_image,
    upload_thumb,
)


def make_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_access_token_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/cgi-bin/token"
        assert request.url.params["appid"] == "wx1"
        assert request.url.params["secret"] == "s1"
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})

    token, expires_in = fetch_access_token("wx1", "s1", client=make_client(handler))
    assert token == "tok"
    assert expires_in == 7200


def test_fetch_access_token_error_40164_appends_whitelist_hint():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 40164, "errmsg": "invalid ip"})

    with pytest.raises(WeChatApiError) as exc_info:
        fetch_access_token("wx1", "s1", client=make_client(handler))
    assert exc_info.value.errcode == 40164
    assert "IP 白名单" in str(exc_info.value)


def test_fetch_access_token_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(WeChatApiError):
        fetch_access_token("wx1", "s1", client=make_client(handler))


def test_upload_thumb_returns_media_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/cgi-bin/material/add_material"
        assert request.url.params["type"] == "thumb"
        assert request.url.params["access_token"] == "tok"
        assert b"cover.jpg" in request.read()
        return httpx.Response(200, json={"media_id": "m1", "url": "http://x"})

    media_id = upload_thumb("tok", "cover.jpg", b"\xff\xd8jpegbytes", client=make_client(handler))
    assert media_id == "m1"


def test_upload_content_image_returns_url():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/cgi-bin/media/uploadimg"
        return httpx.Response(200, json={"url": "https://mmbiz.qpic.cn/x.jpg"})

    url = upload_content_image("tok", "body.png", b"\x89PNGbytes", client=make_client(handler))
    assert url == "https://mmbiz.qpic.cn/x.jpg"


def test_add_draft_returns_media_id_and_posts_utf8():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read().decode("utf-8"))
        return httpx.Response(200, json={"media_id": "draft1"})

    article = build_draft_article(title="标题", content_html="<p>正文</p>", thumb_media_id="m1")
    media_id = add_draft("tok", article, client=make_client(handler))
    assert media_id == "draft1"
    sent = captured["body"]["articles"][0]
    assert sent["title"] == "标题"
    assert sent["thumb_media_id"] == "m1"
    assert sent["digest"] == ""  # 留空：微信自动取正文前 54 字
    assert sent["need_open_comment"] == 0


def test_add_draft_missing_media_id_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    article = build_draft_article(title="t", content_html="<p>x</p>", thumb_media_id="m1")
    with pytest.raises(WeChatApiError):
        add_draft("tok", article, client=make_client(handler))


def test_network_error_wrapped():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(WeChatApiError) as exc_info:
        fetch_access_token("wx1", "s1", client=make_client(handler))
    assert "不可达" in str(exc_info.value)
