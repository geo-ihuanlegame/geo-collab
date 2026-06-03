from pathlib import Path

import pytest

from server.app.modules.articles.parser import BodySegment
from server.app.modules.tasks.drivers.toutiao_html import (
    ImageRef,
    ToutiaoBodyError,
    body_segments_to_toutiao_html,
)


def test_plain_paragraphs():
    segs = [
        BodySegment(kind="text", text="第一段"),
        BodySegment(kind="text", text="\n"),
        BodySegment(kind="text", text="第二段"),
    ]
    html, image_order = body_segments_to_toutiao_html(segs)
    assert html == '<p data-track="1">第一段</p><p data-track="2">第二段</p>'
    assert image_order == []


def test_bold_run_wrapped_in_strong():
    segs = [
        BodySegment(kind="text", text="普通"),
        BodySegment(kind="text", text="加粗", bold=True),
    ]
    html, image_order = body_segments_to_toutiao_html(segs)
    assert html == '<p data-track="1">普通<strong>加粗</strong></p>'
    assert image_order == []


def test_heading_becomes_pgc_subheading():
    # 头条小标题（红点）节点：<h1 class="pgc-h-forward-slash">，不再压成 <p><strong>。
    segs = [BodySegment(kind="text", text="小标题", heading_level=1)]
    html, image_order = body_segments_to_toutiao_html(segs)
    assert html == '<h1 class="pgc-h-forward-slash" data-track="1">小标题</h1>'
    assert image_order == []


def test_h2_maps_to_same_subheading():
    # h1/h2 都映射成同一类头条小标题（符合最初设想）。
    segs = [BodySegment(kind="text", text="二级", heading_level=2)]
    html, image_order = body_segments_to_toutiao_html(segs)
    assert html == '<h1 class="pgc-h-forward-slash" data-track="1">二级</h1>'
    assert image_order == []


def test_heading_then_paragraph_keeps_monotonic_track():
    segs = [
        BodySegment(kind="text", text="小标题", heading_level=1),
        BodySegment(kind="text", text="\n"),
        BodySegment(kind="text", text="正文"),
    ]
    html, image_order = body_segments_to_toutiao_html(segs)
    assert html == (
        '<h1 class="pgc-h-forward-slash" data-track="1">小标题</h1><p data-track="2">正文</p>'
    )
    assert image_order == []


def test_html_special_chars_escaped():
    segs = [BodySegment(kind="text", text="a<b>&c")]
    html, image_order = body_segments_to_toutiao_html(segs)
    assert html == '<p data-track="1">a&lt;b&gt;&amp;c</p>'
    assert image_order == []


def test_empty_body_raises():
    with pytest.raises(ToutiaoBodyError):
        body_segments_to_toutiao_html([])


def test_image_between_text_emits_placeholder_paragraph():
    segs = [
        BodySegment(kind="text", text="a"),
        BodySegment(kind="image", image_asset_id="x", image_path=Path("x.png")),
        BodySegment(kind="text", text="b"),
    ]
    html, image_order = body_segments_to_toutiao_html(segs)
    assert html == (
        '<p data-track="1">a</p><p data-track="2">__GEO_IMG_0__</p><p data-track="3">b</p>'
    )
    assert image_order == [
        ImageRef(token="__GEO_IMG_0__", image_path=Path("x.png"), image_asset_id="x")
    ]
    assert image_order[0].image_asset_id == "x"
    assert image_order[0].image_path == Path("x.png")


def test_two_images_tokens_in_order():
    segs = [
        BodySegment(kind="image", image_asset_id="x", image_path=Path("x.png")),
        BodySegment(kind="text", text="middle"),
        BodySegment(kind="image", stock_image_id=42, image_path=Path("y.png")),
    ]
    html, image_order = body_segments_to_toutiao_html(segs)
    assert html == (
        '<p data-track="1">__GEO_IMG_0__</p>'
        '<p data-track="2">middle</p>'
        '<p data-track="3">__GEO_IMG_1__</p>'
    )
    assert [ref.token for ref in image_order] == ["__GEO_IMG_0__", "__GEO_IMG_1__"]
    assert image_order[0].image_asset_id == "x"
    assert image_order[1].stock_image_id == 42


def test_image_only_article_does_not_raise():
    segs = [BodySegment(kind="image", image_asset_id="solo", image_path=Path("solo.png"))]
    html, image_order = body_segments_to_toutiao_html(segs)
    assert html == '<p data-track="1">__GEO_IMG_0__</p>'
    assert image_order == [
        ImageRef(token="__GEO_IMG_0__", image_path=Path("solo.png"), image_asset_id="solo")
    ]


def test_zero_text_and_zero_images_raises():
    with pytest.raises(ToutiaoBodyError, match="正文为空"):
        body_segments_to_toutiao_html([BodySegment(kind="text", text="\n")])
