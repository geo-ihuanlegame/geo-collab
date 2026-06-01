from server.tests.utils import build_test_app


def create_article(client, title: str) -> int:
    response = client.post(
        "/api/articles",
        json={
            "title": title,
            "content_json": {"type": "doc", "content": []},
        },
    )
    assert response.status_code == 200
    return response.json()["id"]


def test_article_group_crud_and_items_order(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article_1 = create_article(client, "文章 A")
        article_2 = create_article(client, "文章 B")
        article_3 = create_article(client, "文章 C")

        created = client.post(
            "/api/article-groups",
            json={"name": "首批发布", "description": "测试分组"},
        )
        assert created.status_code == 200
        group = created.json()
        assert group["name"] == "首批发布"
        assert group["items"] == []

        updated = client.put(
            f"/api/article-groups/{group['id']}/items",
            json={
                "items": [
                    {"article_id": article_2, "sort_order": 20},
                    {"article_id": article_1, "sort_order": 10},
                    {"article_id": article_3, "sort_order": 30},
                ]
            },
        )
        assert updated.status_code == 200
        assert [item["article_id"] for item in updated.json()["items"]] == [article_1, article_2, article_3]
        assert [item["sort_order"] for item in updated.json()["items"]] == [10, 20, 30]

        renamed = client.put(
            f"/api/article-groups/{group['id']}",
            json={"name": "首批发布-改名", "description": None},
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "首批发布-改名"
        assert renamed.json()["description"] is None

        removed = client.put(
            f"/api/article-groups/{group['id']}/items",
            json={"items": [{"article_id": article_3}]},
        )
        assert removed.status_code == 200
        assert removed.json()["items"] == [{"article_id": article_3, "sort_order": 0}]

        detail = client.get(f"/api/article-groups/{group['id']}")
        assert detail.status_code == 200
        assert detail.json()["items"] == [{"article_id": article_3, "sort_order": 0}]

        list_response = client.get("/api/article-groups")
        assert list_response.status_code == 200
        assert [item["id"] for item in list_response.json()] == [group["id"]]

        delete_response = client.delete(f"/api/article-groups/{group['id']}")
        assert delete_response.status_code == 204
        assert client.get(f"/api/article-groups/{group['id']}").status_code == 404
    finally:
        test_app.cleanup()


def test_article_group_rejects_bad_items_and_duplicate_name(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        article_1 = create_article(client, "文章 A")
        group_1 = client.post("/api/article-groups", json={"name": "重复分组"}).json()

        duplicate_name = client.post("/api/article-groups", json={"name": "重复分组"})
        assert duplicate_name.status_code == 400

        missing_article = client.put(
            f"/api/article-groups/{group_1['id']}/items",
            json={"items": [{"article_id": 9999}]},
        )
        assert missing_article.status_code == 400
        assert "Article not found" in missing_article.json()["detail"]

        duplicate_article = client.put(
            f"/api/article-groups/{group_1['id']}/items",
            json={"items": [{"article_id": article_1}, {"article_id": article_1}]},
        )
        assert duplicate_article.status_code == 400
        assert "Duplicate article_id" in duplicate_article.json()["detail"]
    finally:
        test_app.cleanup()

