"""_resolve_content_body 焦点单测：content_json 透传 + 空回落 plain_text。

标 mysql 是因为 import runner_api 会拉到 db.session（需引擎可构造）；import 放函数内（惰性）
以免 collection 期无 DB 拖垮 shard。无图用例不触 ORM 资产解析。
"""

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.mysql


def _fake_article(*, content_json: str, plain_text: str = ""):
    return SimpleNamespace(content_json=content_json, plain_text=plain_text, body_assets=[])


def test_content_json_passthrough_no_images():
    from server.app.modules.tasks.runner_api import _resolve_content_body

    raw = (
        '{"type":"doc","content":[{"type":"paragraph","content":[{"type":"text","text":"正文"}]}]}'
    )
    content_json, image_paths, temp_files = _resolve_content_body(_fake_article(content_json=raw))
    assert content_json["content"][0]["content"][0]["text"] == "正文"
    assert image_paths == {}
    assert temp_files == []


def test_empty_content_json_falls_back_to_plain_text():
    from server.app.modules.tasks.runner_api import _resolve_content_body

    content_json, image_paths, _ = _resolve_content_body(
        _fake_article(content_json="", plain_text="纯文本兜底")
    )
    assert content_json == {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "纯文本兜底"}]}],
    }
    assert image_paths == {}
