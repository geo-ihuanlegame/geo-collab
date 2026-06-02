import pytest

from server.app.modules.articles.parser import BodySegment
from server.app.modules.tasks.drivers.toutiao_html import (
    ToutiaoBodyError,
    body_segments_to_toutiao_html,
)


def test_plain_paragraphs():
    segs = [
        BodySegment(kind="text", text="第一段"),
        BodySegment(kind="text", text="\n"),
        BodySegment(kind="text", text="第二段"),
    ]
    assert body_segments_to_toutiao_html(segs) == (
        '<p data-track="1">第一段</p><p data-track="2">第二段</p>'
    )


def test_bold_run_wrapped_in_strong():
    segs = [
        BodySegment(kind="text", text="普通"),
        BodySegment(kind="text", text="加粗", bold=True),
    ]
    assert body_segments_to_toutiao_html(segs) == (
        '<p data-track="1">普通<strong>加粗</strong></p>'
    )


def test_heading_becomes_bold_paragraph():
    segs = [BodySegment(kind="text", text="小标题", heading_level=1)]
    assert body_segments_to_toutiao_html(segs) == ('<p data-track="1"><strong>小标题</strong></p>')


def test_html_special_chars_escaped():
    segs = [BodySegment(kind="text", text="a<b>&c")]
    assert body_segments_to_toutiao_html(segs) == ('<p data-track="1">a&lt;b&gt;&amp;c</p>')


def test_empty_body_raises():
    with pytest.raises(ToutiaoBodyError):
        body_segments_to_toutiao_html([])


def test_image_segment_raises_in_m1():
    with pytest.raises(ToutiaoBodyError):
        body_segments_to_toutiao_html([BodySegment(kind="image", image_asset_id="a1")])
