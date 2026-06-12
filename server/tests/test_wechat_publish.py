"""微信驱动 publish_api 测试：MockTransport 全打桩，验证封面回落/转传/HTML 重组/错误映射。

无 DB 用例（驱动纯函数）+ 1 个 mysql 用例（executor 分叉走 API 路径）。
"""

from pathlib import Path

import httpx
import pytest
from PIL import Image

from server.app.modules.articles.parser import BodySegment
from server.app.modules.tasks.drivers.base import ApiPublishPayload, PublishError
from server.app.modules.tasks.drivers.wechat_mp import WeChatMpDriver


def _jpeg_file(tmp_path: Path, name: str, size=(400, 300)) -> Path:
    p = tmp_path / name
    Image.new("RGB", size, (10, 20, 30)).save(p, format="JPEG")
    return p


def _payload(tmp_path: Path, *, cover: Path | None, segments: list[BodySegment]):
    return ApiPublishPayload(
        title="测试标题",
        body_segments=segments,
        cover_path=cover,
        display_name="测试公众号",
        platform_code="wechat_mp",
        access_token="tok",
    )


def _mock_client(uploads: list[str]):
    """打桩三类请求：thumb 上传 → m1；uploadimg → 递增 URL；draft/add → draft-1。"""

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
            return httpx.Response(200, json={"media_id": "draft-1"})
        raise AssertionError(f"unexpected path {path}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_publish_api_full_flow(tmp_path):
    cover = _jpeg_file(tmp_path, "cover.jpg")
    body_img = _jpeg_file(tmp_path, "body.jpg")
    segments = [
        BodySegment(kind="text", text="开头", heading_level=None),
        BodySegment(kind="image", image_asset_id="a1", image_path=body_img),
        BodySegment(kind="text", text="小标题", heading_level=2),
    ]
    uploads: list[str] = []
    driver = WeChatMpDriver()
    result = driver.publish_api(
        payload=_payload(tmp_path, cover=cover, segments=segments),
        client=_mock_client(uploads),
    )
    assert result.url is None
    assert "draft-1" in result.message
    assert uploads == ["thumb", "img", "draft"]


def test_publish_api_cover_fallback_to_first_body_image(tmp_path):
    body_img = _jpeg_file(tmp_path, "body.jpg")
    segments = [BodySegment(kind="image", image_asset_id="a1", image_path=body_img)]
    uploads: list[str] = []
    driver = WeChatMpDriver()
    result = driver.publish_api(
        payload=_payload(tmp_path, cover=None, segments=segments),
        client=_mock_client(uploads),
    )
    assert "draft-1" in result.message
    assert "thumb" in uploads  # 正文首图被用作封面上传


def test_publish_api_no_image_at_all_raises(tmp_path):
    segments = [BodySegment(kind="text", text="只有文字")]
    driver = WeChatMpDriver()
    with pytest.raises(PublishError, match="封面"):
        driver.publish_api(
            payload=_payload(tmp_path, cover=None, segments=segments),
            client=_mock_client([]),
        )


def test_publish_api_wechat_error_mapped_to_publish_error(tmp_path):
    cover = _jpeg_file(tmp_path, "cover.jpg")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 45009, "errmsg": "api freq out of limit"})

    driver = WeChatMpDriver()
    with pytest.raises(PublishError, match="45009"):
        driver.publish_api(
            payload=_payload(tmp_path, cover=cover, segments=[BodySegment(kind="text", text="x")]),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )


def test_segments_to_html_headings_and_bold():
    from server.app.modules.tasks.drivers.wechat_mp import segments_to_html

    segments = [
        BodySegment(kind="text", text="大标题", heading_level=1),
        BodySegment(kind="text", text="加粗", bold=True),
        BodySegment(kind="text", text="普通段落"),
        BodySegment(kind="image", image_asset_id="a1"),
    ]
    html = segments_to_html(segments, {3: "https://mmbiz.qpic.cn/1.jpg"})
    assert "<h1>大标题</h1>" in html
    assert "<p><strong>加粗</strong></p>" in html
    assert "<p>普通段落</p>" in html
    assert '<img src="https://mmbiz.qpic.cn/1.jpg"' in html


def test_segments_to_html_escapes_text():
    from server.app.modules.tasks.drivers.wechat_mp import segments_to_html

    html = segments_to_html([BodySegment(kind="text", text="a<b>&c")], {})
    assert "a&lt;b&gt;&amp;c" in html
