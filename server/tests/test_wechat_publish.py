"""微信驱动 publish_api 测试：MockTransport 全打桩，验证保真 HTML / 封面回落 / 错误映射。

驱动纯函数，无 DB。payload 直接给 content_json + image_paths（对齐 Task 3 后的载荷形态）。
"""

from pathlib import Path

import httpx
import pytest
from PIL import Image

from server.app.modules.tasks.drivers.base import ApiPublishPayload, PublishError
from server.app.modules.tasks.drivers.wechat_mp import WeChatMpDriver


def _jpeg_file(tmp_path: Path, name: str, size=(400, 300)) -> Path:
    p = tmp_path / name
    Image.new("RGB", size, (10, 20, 30)).save(p, format="JPEG")
    return p


def _payload(*, cover, content_json, image_paths):
    return ApiPublishPayload(
        title="测试标题",
        body_segments=[],
        cover_path=cover,
        display_name="测试公众号",
        platform_code="wechat_mp",
        access_token="tok",
        content_json=content_json,
        image_paths=image_paths,
    )


def _mock_client(uploads: list[str], captured: dict):
    """打桩：thumb→m1；uploadimg→递增 URL；draft/add→记录正文 body 后回 draft-1。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/cgi-bin/material/add_material":
            uploads.append("thumb")
            return httpx.Response(200, json={"media_id": "m1"})
        if path == "/cgi-bin/media/uploadimg":
            uploads.append("img")
            return httpx.Response(200, json={"url": f"https://mmbiz.qpic.cn/{len(uploads)}.jpg"})
        if path == "/cgi-bin/draft/add":
            uploads.append("draft")
            captured["body"] = request.content.decode("utf-8")
            return httpx.Response(200, json={"media_id": "draft-1"})
        raise AssertionError(f"unexpected path {path}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_publish_api_faithful_html_and_order(tmp_path):
    body_img = _jpeg_file(tmp_path, "body.jpg")
    cover = _jpeg_file(tmp_path, "cover.jpg")
    content_json = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 3},
                "content": [{"type": "text", "text": "小标题"}],
            },
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "条目"}]}
                        ],
                    }
                ],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "斜", "marks": [{"type": "italic"}]}],
            },
            {"type": "image", "attrs": {"assetId": "a1"}},
        ],
    }
    uploads: list[str] = []
    captured: dict = {}
    result = WeChatMpDriver().publish_api(
        payload=_payload(cover=cover, content_json=content_json, image_paths={"a1": body_img}),
        client=_mock_client(uploads, captured),
    )
    assert result.url is None
    assert "draft-1" in result.message
    assert uploads == ["thumb", "img", "draft"]
    # 草稿正文保住了被旧链路丢掉的格式
    assert "<h3>小标题</h3>" in captured["body"]
    assert "<ul><li>条目</li></ul>" in captured["body"]
    assert "<em>斜</em>" in captured["body"]
    assert "https://mmbiz.qpic.cn/2.jpg" in captured["body"]  # uploadimg 换好的图 url


def test_publish_api_cover_fallback_to_first_body_image(tmp_path):
    body_img = _jpeg_file(tmp_path, "body.jpg")
    content_json = {"type": "doc", "content": [{"type": "image", "attrs": {"assetId": "a1"}}]}
    uploads: list[str] = []
    captured: dict = {}
    result = WeChatMpDriver().publish_api(
        payload=_payload(cover=None, content_json=content_json, image_paths={"a1": body_img}),
        client=_mock_client(uploads, captured),
    )
    assert "draft-1" in result.message
    assert "thumb" in uploads  # 正文首图被用作封面上传


def test_publish_api_no_image_at_all_raises(tmp_path):
    content_json = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "只有文字"}]}],
    }
    with pytest.raises(PublishError, match="封面"):
        WeChatMpDriver().publish_api(
            payload=_payload(cover=None, content_json=content_json, image_paths={}),
            client=_mock_client([], {}),
        )


def test_publish_api_wechat_error_mapped_to_publish_error(tmp_path):
    cover = _jpeg_file(tmp_path, "cover.jpg")
    content_json = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "x"}]}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 45009, "errmsg": "api freq out of limit"})

    with pytest.raises(PublishError, match="45009"):
        WeChatMpDriver().publish_api(
            payload=_payload(cover=cover, content_json=content_json, image_paths={}),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
