from __future__ import annotations

from pathlib import Path

import pytest

from server.app.modules.articles.parser import BodySegment
from server.app.modules.tasks.drivers.base import PublishError, PublishPayload, PublishResult


def _make_payload(**overrides) -> PublishPayload:
    defaults = dict(
        title="Test",
        cover_asset_path=Path("/tmp/cover.jpg"),
        body_segments=[],
        account_key="toutiao/test",
        state_path=Path("/tmp/state.json"),
        display_name="测试账号",
        platform_code="toutiao",
    )
    defaults.update(overrides)
    return PublishPayload(**defaults)


def test_publish_payload_is_frozen():
    payload = _make_payload(body_segments=[BodySegment(kind="text", text="Hello")])
    with pytest.raises((TypeError, AttributeError)):
        payload.title = "Modified"  # type: ignore


def test_publish_payload_cover_path():
    payload = _make_payload(cover_asset_path=Path("/data/assets/img.jpg"))
    assert payload.cover_asset_path == Path("/data/assets/img.jpg")


def test_body_segment_image_has_path():
    seg = BodySegment(kind="image", image_path=Path("/tmp/img.jpg"), image_asset_id="abc")
    assert seg.image_path == Path("/tmp/img.jpg")
    assert seg.image_asset_id == "abc"
    assert seg.text == ""


def test_publish_result_fields():
    result = PublishResult(url="https://example.com", title="T", message="ok")
    assert result.url == "https://example.com"


def test_publish_error_carries_screenshot():
    err = PublishError("失败", screenshot=b"\x89PNG")
    assert str(err) == "失败"
    assert err.screenshot == b"\x89PNG"


def test_publish_error_no_screenshot():
    err = PublishError("无截图")
    assert err.screenshot is None
