"""文章审核 + 分组审核进度 API 测试（A1 scope）。

需要 MySQL（GEO_TEST_DATABASE_URL）；未设置时由 @pytest.mark.mysql 自动跳过。
"""

import pytest

from server.tests.utils import build_test_app


def _create_article(client, title: str = "审核测试文章") -> dict:
    response = client.post(
        "/api/articles",
        json={
            "title": title,
            "content_html": "<p>hello</p>",
            "plain_text": "hello",
            "word_count": 5,
            "status": "draft",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _create_group(client, name: str = "审核测试分组") -> dict:
    response = client.post("/api/article-groups", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.mysql
def test_new_article_defaults_to_approved(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        article = _create_article(client)
        # 手工新建文章默认 approved（server_default）。
        assert article["review_status"] == "approved"

        detail = client.get(f"/api/articles/{article['id']}")
        assert detail.status_code == 200
        assert detail.json()["review_status"] == "approved"
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_revoke_and_approve_article(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        article = _create_article(client)
        aid = article["id"]
        base_version = article["version"]

        # 撤销审核 → pending，version+1
        revoked = client.post(f"/api/articles/{aid}/revoke-approval")
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["review_status"] == "pending"
        assert revoked.json()["version"] == base_version + 1

        # 再通过审核 → approved，version+1
        approved = client.post(f"/api/articles/{aid}/approve")
        assert approved.status_code == 200, approved.text
        assert approved.json()["review_status"] == "approved"
        assert approved.json()["version"] == base_version + 2
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_approve_missing_article_returns_404(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        resp = client.post("/api/articles/999999/approve")
        assert resp.status_code == 404
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_list_filter_by_review_status(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        a1 = _create_article(client, "已审核文章")
        a2 = _create_article(client, "未审核文章")
        client.post(f"/api/articles/{a2['id']}/revoke-approval")

        approved = client.get("/api/articles", params={"review_status": "approved"})
        assert approved.status_code == 200
        approved_ids = {a["id"] for a in approved.json()}
        assert a1["id"] in approved_ids
        assert a2["id"] not in approved_ids

        pending = client.get("/api/articles", params={"review_status": "pending"})
        assert pending.status_code == 200
        pending_ids = {a["id"] for a in pending.json()}
        assert a2["id"] in pending_ids
        assert a1["id"] not in pending_ids

        # 无过滤 → 两篇都在
        all_resp = client.get("/api/articles")
        all_ids = {a["id"] for a in all_resp.json()}
        assert a1["id"] in all_ids and a2["id"] in all_ids
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_invalid_review_status_returns_400(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        resp = client.get("/api/articles", params={"review_status": "bogus"})
        assert resp.status_code == 400
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_group_review_summary_and_approve_all(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        a1 = _create_article(client, "组内文章1")
        a2 = _create_article(client, "组内文章2")
        # 都打回未审核
        client.post(f"/api/articles/{a1['id']}/revoke-approval")
        client.post(f"/api/articles/{a2['id']}/revoke-approval")

        group = _create_group(client)
        gid = group["id"]
        # 初始组无 item → summary total=0
        assert group["review_summary"] == {"total": 0, "approved": 0}

        items_resp = client.put(
            f"/api/article-groups/{gid}/items",
            json={
                "items": [
                    {"article_id": a1["id"], "sort_order": 0},
                    {"article_id": a2["id"], "sort_order": 1},
                ]
            },
        )
        assert items_resp.status_code == 200, items_resp.text
        assert items_resp.json()["review_summary"] == {"total": 2, "approved": 0}

        # 先单独审核一篇 → 1/2
        client.post(f"/api/articles/{a1['id']}/approve")
        read_group = client.get(f"/api/article-groups/{gid}")
        assert read_group.json()["review_summary"] == {"total": 2, "approved": 1}

        # 整组通过 → 2/2
        approve_all = client.post(f"/api/article-groups/{gid}/approve-all")
        assert approve_all.status_code == 200, approve_all.text
        assert approve_all.json()["review_summary"] == {"total": 2, "approved": 2}

        # 列表里组也带 summary
        list_resp = client.get("/api/article-groups")
        target = next(g for g in list_resp.json() if g["id"] == gid)
        assert target["review_summary"] == {"total": 2, "approved": 2}
    finally:
        test_app.cleanup()
