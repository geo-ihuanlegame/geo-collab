"""TapTap 驱动 publish_api 全流程测试（httpx MockTransport 路由 taptap + 七牛，无 DB / 无网络）。

覆盖：图片传七牛→url 代入 contents→create-topic→publish-topic；鉴权头/X-UA 查询参；
以及缺登录态 / 缺论坛配置 / 正文空 的失败路径。
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from server.app.modules.tasks.drivers.base import ApiPublishPayload, PublishError
from server.app.modules.tasks.drivers.taptap import TapTapDriver

DRIVER = TapTapDriver()

_STATE = {
    "cookies": [
        {"name": "XSRF-TOKEN", "value": "tok%20en", "domain": ".taptap.cn"},
        {"name": "sess", "value": "abc", "domain": "www.taptap.cn"},
    ]
}
_FORUM = {"app_id": 43639, "group_id": 4444, "x_ua": "V=1&PN=WebApp&VID=780586114"}


def _doc_with_image():
    return {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "正文一段"}]},
            {"type": "image", "attrs": {"assetId": "a1"}},
        ],
    }


def _make_payload(tmp_path, *, state=_STATE, forum=_FORUM, content_json=None, with_image=True):
    image_paths = {}
    if with_image:
        img = tmp_path / "pic.jpg"
        img.write_bytes(b"\xff\xd8\xff\xfake-jpeg")
        image_paths = {"a1": img}
    return ApiPublishPayload(
        title="测试长帖",
        body_segments=[],
        cover_path=None,
        display_name="测试账号",
        platform_code="taptap",
        state=state,
        forum=forum,
        content_json=content_json if content_json is not None else _doc_with_image(),
        image_paths=image_paths,
    )


def _transport(captured):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        host = request.url.host
        path = request.url.path
        if host == "upload.qiniup.com":
            return httpx.Response(
                200, json={"url": "https://img2-tc.tapimg.com/moment/x.jpg", "info": {"size": 9}}
            )
        if path.endswith("image-upload-token"):
            return httpx.Response(200, json={"success": True, "data": {"token": "qntoken"}})
        if path.endswith("create-topic"):
            return httpx.Response(
                200, json={"success": True, "data": {"moment_draft": {"id_str": "D123"}}}
            )
        if path.endswith("publish-topic"):
            return httpx.Response(
                200, json={"success": True, "data": {"moment": {"id_str": "M999"}}}
            )
        return httpx.Response(404, json={"success": False, "msg": "unexpected"})

    return httpx.MockTransport(handler)


def test_publish_full_flow(tmp_path):
    captured: list[httpx.Request] = []
    result = DRIVER.publish_api(payload=_make_payload(tmp_path), transport=_transport(captured))

    assert result.url == "https://www.taptap.cn/moment/M999"
    assert result.title == "测试长帖"

    # 命中四个端点（顺序：token → 七牛 → create → publish）
    paths = [r.url.path.split("/")[-1] for r in captured]
    assert paths == ["image-upload-token", "", "create-topic", "publish-topic"]

    taptap_reqs = [r for r in captured if r.url.host == "www.taptap.cn"]
    for r in taptap_reqs:
        assert r.url.params.get("X-UA") == _FORUM["x_ua"]
        assert r.headers.get("X-XSRF-TOKEN") == "tok en"  # unquote 后

    create = next(r for r in captured if r.url.path.endswith("create-topic"))
    form = {k: v[0] for k, v in parse_qs(create.content.decode()).items()}
    contents = json.loads(form["contents"])
    # 段落 + 图片块（图片 url 已代入）
    assert {"type": "paragraph", "children": [{"text": "正文一段"}]} in contents
    assert {
        "type": "image",
        "info": {"img_url": "https://img2-tc.tapimg.com/moment/x.jpg", "description": ""},
    } in contents
    # forum_bindings 带 group_id，image_infos 带 url
    assert json.loads(form["forum_bindings"])[0]["group_id"] == 4444
    assert json.loads(form["image_infos"])[0]["url"] == "https://img2-tc.tapimg.com/moment/x.jpg"

    publish = next(r for r in captured if r.url.path.endswith("publish-topic"))
    pform = {k: v[0] for k, v in parse_qs(publish.content.decode()).items()}
    assert pform["id"] == "D123"


def test_publish_without_images(tmp_path):
    captured: list[httpx.Request] = []
    payload = _make_payload(
        tmp_path,
        with_image=False,
        content_json={
            "type": "doc",
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "纯文字"}]}],
        },
    )
    result = DRIVER.publish_api(payload=payload, transport=_transport(captured))
    assert result.url == "https://www.taptap.cn/moment/M999"
    # 无图：不调七牛 / token
    assert not any(r.url.host == "upload.qiniup.com" for r in captured)


def test_missing_xsrf_cookie_raises(tmp_path):
    bad_state = {"cookies": [{"name": "sess", "value": "x", "domain": ".taptap.cn"}]}
    with pytest.raises(PublishError, match="XSRF"):
        DRIVER.publish_api(
            payload=_make_payload(tmp_path, state=bad_state), transport=_transport([])
        )


def test_missing_forum_config_raises(tmp_path):
    with pytest.raises(PublishError, match="论坛绑定"):
        DRIVER.publish_api(
            payload=_make_payload(tmp_path, forum={"app_id": 1}), transport=_transport([])
        )


def test_missing_state_raises(tmp_path):
    with pytest.raises(PublishError, match="登录态"):
        DRIVER.publish_api(payload=_make_payload(tmp_path, state=None), transport=_transport([]))


def test_empty_contents_raises(tmp_path):
    payload = _make_payload(tmp_path, with_image=False, content_json={"type": "doc", "content": []})
    with pytest.raises(PublishError, match="正文为空"):
        DRIVER.publish_api(payload=payload, transport=_transport([]))


def test_api_error_surfaced_as_publish_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("create-topic"):
            return httpx.Response(200, json={"success": False, "data": {"msg": "版块不存在"}})
        if request.url.path.endswith("image-upload-token"):
            return httpx.Response(200, json={"success": True, "data": {"token": "t"}})
        if request.url.host == "upload.qiniup.com":
            return httpx.Response(200, json={"url": "https://img2-tc.tapimg.com/x.jpg", "info": {}})
        return httpx.Response(200, json={"success": True, "data": {}})

    with pytest.raises(PublishError, match="版块不存在"):
        DRIVER.publish_api(payload=_make_payload(tmp_path), transport=httpx.MockTransport(handler))


def test_401_surfaced_as_publish_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("image-upload-token"):
            return httpx.Response(401, json={"success": False})
        return httpx.Response(200, json={"success": True, "data": {}})

    with pytest.raises(PublishError, match="cookie 失效|401"):
        DRIVER.publish_api(payload=_make_payload(tmp_path), transport=httpx.MockTransport(handler))
