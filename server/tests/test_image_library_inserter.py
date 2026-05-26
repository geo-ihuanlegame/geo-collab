from __future__ import annotations

from server.app.modules.image_library.inserter import insert_images_at_positions
from server.app.modules.image_library.selector import StockImageRef


def _content():
    return {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "before"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "after"}]},
        ],
    }


def test_insert_stock_image_adds_official_url_paragraph():
    ref = StockImageRef(
        id=42,
        url="/api/stock-images/42/file",
        filename="game.jpg",
        width=800,
        height=600,
        category_id=7,
        official_url="https://game.example.com",
    )

    result = insert_images_at_positions(_content(), [ref], [0])
    inserted_image = result["content"][1]
    inserted_url = result["content"][2]

    assert inserted_image["type"] == "image"
    assert inserted_image["attrs"]["stockImageId"] == 42
    assert inserted_url == {
        "type": "paragraph",
        "content": [{"type": "text", "text": "https://game.example.com"}],
    }


def test_insert_stock_image_without_official_url_only_adds_image():
    ref = StockImageRef(
        id=42,
        url="/api/stock-images/42/file",
        filename="game.jpg",
        width=800,
        height=600,
        category_id=7,
        official_url=None,
    )

    result = insert_images_at_positions(_content(), [ref], [0])

    assert [node["type"] for node in result["content"]] == ["paragraph", "image", "paragraph"]
