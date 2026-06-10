"""微信公众号账号 API 测试：创建校验、secret 掩码、verify-credentials、PATCH、浏览器流守卫。"""

import json
import zipfile
from io import BytesIO

import pytest

from server.app.modules.tasks.drivers.wechat_client import WeChatApiError
from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


def _ensure_wechat_platform(test_app) -> None:
    from server.app.modules.system.models import Platform

    with test_app.session_factory() as db:
        if db.query(Platform).filter(Platform.code == "wechat_mp").first() is None:
            db.add(
                Platform(
                    code="wechat_mp",
                    name="微信公众号",
                    base_url="https://mp.weixin.qq.com",
                    enabled=True,
                )
            )
            db.commit()


def _create_payload(**overrides):
    payload = {
        "platform_code": "wechat_mp",
        "display_name": "测试公众号",
        "api_credentials": {"app_id": "wx8f2a91c0d3e5b6", "app_secret": "secret-end-3a7f"},
        "contact": "186***3027",
        "note": "主力号",
    }
    payload.update(overrides)
    return payload


def _make_approved_article(client, title="公众号文章"):
    resp = client.post(
        "/api/articles",
        json={
            "title": title,
            "content_json": {"type": "doc", "content": []},
            "content_html": "<p>x</p>",
            "plain_text": "x",
            "word_count": 1,
            "status": "ready",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def test_create_wechat_account_masks_secret(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        resp = test_app.client.post("/api/accounts", json=_create_payload())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["platform_code"] == "wechat_mp"
        assert body["app_id"] == "wx8f2a91c0d3e5b6"
        assert body["app_secret_tail"] == "3a7f"
        assert body["state_path"] is None
        assert body["distribution_enabled"] is True
        assert body["platform_user_id"] == "wx8f2a91c0d3e5b6"
        assert "api_credentials" not in body
    finally:
        test_app.cleanup()


def test_platforms_lists_wechat_api_platform_before_driver_registration(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        resp = test_app.client.get("/api/accounts/platforms")
        assert resp.status_code == 200
        assert {"code": "wechat_mp", "name": "微信公众号"} in resp.json()
    finally:
        test_app.cleanup()


def test_create_duplicate_app_id_conflict(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        assert test_app.client.post("/api/accounts", json=_create_payload()).status_code == 200
        resp = test_app.client.post("/api/accounts", json=_create_payload())
        assert resp.status_code == 409
    finally:
        test_app.cleanup()


def test_create_soft_deleted_duplicate_app_id_conflict(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        account_id = test_app.client.post("/api/accounts", json=_create_payload()).json()["id"]
        assert test_app.client.delete(f"/api/accounts/{account_id}").status_code == 204

        resp = test_app.client.post("/api/accounts", json=_create_payload())
        assert resp.status_code == 409
    finally:
        test_app.cleanup()


def test_create_browser_platform_rejected(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        resp = test_app.client.post("/api/accounts", json=_create_payload(platform_code="toutiao"))
        assert resp.status_code == 400
    finally:
        test_app.cleanup()


def test_verify_credentials_success_sets_valid_and_caches_token(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        account_id = test_app.client.post("/api/accounts", json=_create_payload()).json()["id"]

        monkeypatch.setattr(
            "server.app.modules.accounts.service.wechat_fetch_access_token",
            lambda app_id, app_secret, client=None: ("tok-1", 7200),
        )
        resp = test_app.client.post(f"/api/accounts/{account_id}/verify-credentials")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "valid"

        from server.app.modules.accounts.models import Account

        with test_app.session_factory() as db:
            cache = db.get(Account, account_id).api_token_cache
        assert cache["access_token"] == "tok-1"
        assert cache["expires_at"] > 0
    finally:
        test_app.cleanup()


def test_verify_credentials_failure_sets_expired_and_returns_hint(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        account_id = test_app.client.post("/api/accounts", json=_create_payload()).json()["id"]

        def boom(app_id, app_secret, client=None):
            raise WeChatApiError(
                "微信接口错误 40164: invalid ip（请把服务器出口公网 IP 加入…）",
                errcode=40164,
            )

        monkeypatch.setattr("server.app.modules.accounts.service.wechat_fetch_access_token", boom)
        resp = test_app.client.post(f"/api/accounts/{account_id}/verify-credentials")
        assert resp.status_code == 400
        assert "40164" in resp.json()["detail"]

        from server.app.modules.accounts.models import Account

        with test_app.session_factory() as db:
            assert db.get(Account, account_id).status == "expired"
    finally:
        test_app.cleanup()


def test_failed_reverify_clears_token_even_if_audit_rolls_back(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        account_id = test_app.client.post("/api/accounts", json=_create_payload()).json()["id"]

        monkeypatch.setattr(
            "server.app.modules.accounts.service.wechat_fetch_access_token",
            lambda app_id, app_secret, client=None: ("tok-1", 7200),
        )
        assert (
            test_app.client.post(f"/api/accounts/{account_id}/verify-credentials").status_code
            == 200
        )

        def boom(app_id, app_secret, client=None):
            raise WeChatApiError("微信接口错误 40001: invalid credential", errcode=40001)

        monkeypatch.setattr("server.app.modules.accounts.service.wechat_fetch_access_token", boom)

        def audit_rollback(db, **_kwargs):
            db.rollback()

        monkeypatch.setattr("server.app.modules.accounts.router.add_audit_entry", audit_rollback)
        resp = test_app.client.post(f"/api/accounts/{account_id}/verify-credentials")
        assert resp.status_code == 400

        from server.app.modules.accounts.models import Account
        from server.app.modules.accounts.service import get_cached_wechat_token

        with test_app.session_factory() as db:
            account = db.get(Account, account_id)
            assert account.status == "expired"
            assert account.api_token_cache is None
            assert get_cached_wechat_token(account) is None
    finally:
        test_app.cleanup()


def test_patch_updates_fields_and_replaces_secret(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        account_id = test_app.client.post("/api/accounts", json=_create_payload()).json()["id"]
        monkeypatch.setattr(
            "server.app.modules.accounts.service.wechat_fetch_access_token",
            lambda app_id, app_secret, client=None: ("tok-1", 7200),
        )
        assert (
            test_app.client.post(f"/api/accounts/{account_id}/verify-credentials").json()["status"]
            == "valid"
        )

        resp = test_app.client.patch(
            f"/api/accounts/{account_id}",
            json={
                "display_name": "云栖",
                "distribution_enabled": False,
                "api_credentials": {
                    "app_id": "wx8f2a91c0d3e5b6",
                    "app_secret": "new-secret-9b2c",
                },
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["display_name"] == "云栖"
        assert body["distribution_enabled"] is False
        assert body["app_secret_tail"] == "9b2c"
        assert body["status"] == "unknown"

        from server.app.modules.accounts.models import Account

        with test_app.session_factory() as db:
            assert db.get(Account, account_id).api_token_cache is None
    finally:
        test_app.cleanup()


def test_patch_rename_only_still_works(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        account_id = test_app.client.post("/api/accounts", json=_create_payload()).json()["id"]
        resp = test_app.client.patch(f"/api/accounts/{account_id}", json={"display_name": "改名"})
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "改名"
        assert resp.json()["app_secret_tail"] == "3a7f"
    finally:
        test_app.cleanup()


def test_api_account_browser_flows_rejected(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        account_id = test_app.client.post("/api/accounts", json=_create_payload()).json()["id"]

        login_session = test_app.client.post(f"/api/accounts/{account_id}/login-session")
        assert login_session.status_code == 400

        check = test_app.client.post(
            f"/api/accounts/{account_id}/check", json={"use_browser": False}
        )
        assert check.status_code == 400

        platform_session = test_app.client.post(
            "/api/accounts/wechat_mp/login-session",
            json={"display_name": "wechat", "account_key": "x"},
        )
        assert platform_session.status_code == 400
    finally:
        test_app.cleanup()


def test_export_all_skips_missing_browser_state_for_api_account(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        account_id = test_app.client.post("/api/accounts", json=_create_payload()).json()["id"]

        resp = test_app.client.post("/api/accounts/export", json={})
        assert resp.status_code == 200, resp.text

        with zipfile.ZipFile(BytesIO(resp.content)) as archive:
            account_dir = f"accounts/wechat_mp-{account_id}"
            assert f"{account_dir}/account.json" in archive.namelist()
            assert f"{account_dir}/storage_state.json" not in archive.namelist()
            account_payload = json.loads(archive.read(f"{account_dir}/account.json"))
            assert account_payload["state_path"] is None
    finally:
        test_app.cleanup()


def test_verified_api_account_rejected_by_browser_task_path_until_runner_exists(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        article_id = _make_approved_article(test_app.client)
        account_id = test_app.client.post("/api/accounts", json=_create_payload()).json()["id"]
        monkeypatch.setattr(
            "server.app.modules.accounts.service.wechat_fetch_access_token",
            lambda app_id, app_secret, client=None: ("tok-1", 7200),
        )
        assert (
            test_app.client.post(f"/api/accounts/{account_id}/verify-credentials").status_code
            == 200
        )

        from server.app.modules.articles.models import Article
        from server.app.modules.tasks.schemas import TaskAccountInput, TaskCreate
        from server.app.modules.tasks.service import create_task
        from server.app.shared.errors import AccountError

        with test_app.session_factory() as db:
            uid = db.get(Article, article_id).user_id
            payload = TaskCreate(
                name="公众号发布",
                task_type="single",
                article_id=article_id,
                platform_code="wechat_mp",
                accounts=[TaskAccountInput(account_id=account_id)],
                stop_before_publish=False,
            )
            with pytest.raises(AccountError, match="API-only"):
                create_task(db, uid, payload, role="admin")
    finally:
        test_app.cleanup()
