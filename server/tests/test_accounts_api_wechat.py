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
        # API 平台身份 = AppID（建号即写），不应误挂「身份未知」徽标（identity_known 只看 platform_user_id）
        assert body["identity_known"] is True
        assert "api_credentials" not in body
    finally:
        test_app.cleanup()


def test_platforms_lists_wechat_api_platform_before_driver_registration(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        resp = test_app.client.get("/api/accounts/platforms")
        assert resp.status_code == 200
        assert {"code": "wechat_mp", "name": "微信公众号", "mode": "api"} in resp.json()
        # 浏览器登录平台（如头条）应标记 browser，供前端区分凭据直填 / 扫码登录
        assert all(p["mode"] in {"api", "browser"} for p in resp.json()), "每个平台都应带 mode 标记"
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


def test_list_accounts_fuzzy_search_q(monkeypatch):
    """GET /api/accounts?q= 泛搜索：账号名称 / 备注 / 手机号 任一包含命中。"""
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        specs = [
            ("主力公众号", "运营部主力号", "13800001111", "wxAAAA0000000001"),
            ("备用号", "测试用途", "13900002222", "wxBBBB0000000002"),
            ("财经号", "归属张三", "18612340000", "wxCCCC0000000003"),
        ]
        ids = {}
        for name, note, contact, app_id in specs:
            resp = test_app.client.post(
                "/api/accounts",
                json=_create_payload(
                    display_name=name,
                    note=note,
                    contact=contact,
                    api_credentials={"app_id": app_id, "app_secret": "secret-end-3a7f"},
                ),
            )
            assert resp.status_code == 200, resp.text
            ids[name] = resp.json()["id"]

        def search(q):
            resp = test_app.client.get("/api/accounts", params={"q": q})
            assert resp.status_code == 200, resp.text
            return {row["id"] for row in resp.json()}

        # 无 q / 空白 q → 全量
        assert len(search("")) == 3
        assert len(test_app.client.get("/api/accounts").json()) == 3
        # 命中账号名称（也命中备注）
        assert search("主力") == {ids["主力公众号"]}
        # 命中备注
        assert search("测试") == {ids["备用号"]}
        assert search("张三") == {ids["财经号"]}
        # 命中手机号
        assert search("13800") == {ids["主力公众号"]}
        # 跨多账号包含
        assert search("号") == set(ids.values())
        # 无命中
        assert search("不存在xyz") == set()
    finally:
        test_app.cleanup()


def test_create_after_soft_deleted_app_id_succeeds(monkeypatch):
    """删除账号后身份槽位已释放（platform_user_id=None），同一 app_id 可重新登记。"""
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        account_id = test_app.client.post("/api/accounts", json=_create_payload()).json()["id"]
        assert test_app.client.delete(f"/api/accounts/{account_id}").status_code == 204

        resp = test_app.client.post("/api/accounts", json=_create_payload())
        assert resp.status_code == 200
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


def test_delete_wechat_account_frees_identity_slot(monkeypatch):
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

        assert test_app.client.delete(f"/api/accounts/{account_id}").status_code == 204

        from server.app.modules.accounts.models import Account

        with test_app.session_factory() as db:
            acc = db.get(Account, account_id)  # db.get 不过滤 is_deleted，能取到死行
            assert acc.is_deleted is True
            assert acc.deleted_at is not None
            assert acc.platform_user_id is None
            assert acc.api_token_cache is None
            creds = acc.api_credentials or {}
            assert "app_secret" not in creds  # 密钥已抹除
            assert creds.get("app_id") == "wx8f2a91c0d3e5b6"  # app_id 保留供审计
    finally:
        test_app.cleanup()


def test_app_id_globally_unique_across_users(monkeypatch):
    """一个 app_id 全平台只能活一份：A 用户登记后，B 用户登记同一 app_id 应 409。"""
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        assert test_app.client.post("/api/accounts", json=_create_payload()).status_code == 200

        from server.tests.utils import create_extra_user

        _uid, other_client = create_extra_user(test_app, "operator2")
        resp = other_client.post("/api/accounts", json=_create_payload())
        assert resp.status_code == 409, resp.text
    finally:
        test_app.cleanup()


def test_verified_api_account_accepted_by_task_path(monkeypatch):
    """公众号(API)账号经校验(status=valid)后可直接建发布任务——终点为草稿箱，无需浏览器 state_path。

    曾经任务层硬拒 API 账号（占位用例 *_until_runner_exists 断言抛 AccountError "API-only"）；
    distribute 支持 API 平台分发后，任务层对 mode='api' 平台放行无 state_path 的账号，遂翻转此用例。
    """
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
        from server.app.modules.tasks.models import PublishRecord, PublishTask
        from server.app.modules.tasks.schemas import TaskAccountInput, TaskCreate
        from server.app.modules.tasks.service import create_task

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
            task = create_task(db, uid, payload, role="admin")
            db.commit()
            tid = task.id

        with test_app.session_factory() as db:
            assert db.get(PublishTask, tid) is not None
            recs = db.query(PublishRecord).filter(PublishRecord.task_id == tid).all()
            assert len(recs) == 1
            assert recs[0].account_id == account_id  # 公众号账号成功落记录（草稿箱路径）
    finally:
        test_app.cleanup()


def test_run_publish_api_with_detached_account_resolves_platform(monkeypatch):
    """回归 #90：API 发布在发布线程里（detached account）读 account.platform.code 会触发懒加载。

    PR#70(#78) 的 detached 回归只覆盖 record.platform（build runner 阶段）；run_publish_api /
    _build_api_payload 读的是 account.platform，从未被 detached 路径覆盖，遂裸奔到生产
    —— 公众号(API)发布 100% 失败于 DetachedInstanceError。本用例走 build_publish_runner_for_record
    → 调 runner 的完整 API 分叉，token 解析与 driver.publish_api 打桩，断言不再懒加载 platform。
    """
    from sqlalchemy.orm.exc import DetachedInstanceError

    from server.app.modules.accounts.models import Account
    from server.app.modules.articles.models import Article
    from server.app.modules.tasks.drivers.base import PublishResult
    from server.app.modules.tasks.drivers.wechat_mp import WeChatMpDriver
    from server.app.modules.tasks.executor import (
        _detach_record_inputs,
        _load_article_for_publish,
        build_publish_runner_for_record,
    )
    from server.app.modules.tasks.models import PublishRecord
    from server.app.modules.tasks.schemas import TaskAccountInput, TaskCreate
    from server.app.modules.tasks.service import create_task

    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        _ensure_wechat_platform(test_app)
        article_id = _make_approved_article(client)
        account_id = client.post("/api/accounts", json=_create_payload()).json()["id"]
        monkeypatch.setattr(
            "server.app.modules.accounts.service.wechat_fetch_access_token",
            lambda app_id, app_secret, client=None: ("tok-1", 7200),
        )
        assert client.post(f"/api/accounts/{account_id}/verify-credentials").status_code == 200

        with test_app.session_factory() as db:
            uid = db.get(Article, article_id).user_id
            task = create_task(
                db,
                uid,
                TaskCreate(
                    name="公众号 detached 回归",
                    task_type="single",
                    article_id=article_id,
                    platform_code="wechat_mp",
                    accounts=[TaskAccountInput(account_id=account_id)],
                    stop_before_publish=False,
                ),
                role="admin",
            )
            db.commit()
            task_id = task.id

        # 打桩 token 解析与驱动发布，避免真打微信接口
        monkeypatch.setattr(
            "server.app.modules.tasks.runner_api._resolve_access_token",
            lambda account_id: "tok-stub",
        )
        captured: dict = {}

        def fake_publish_api(self, *, payload, client=None, commit_guard=None, retry_policy=None):
            captured["platform_code"] = payload.platform_code
            return PublishResult(url=None, title=payload.title, message="draft-ok")

        monkeypatch.setattr(WeChatMpDriver, "publish_api", fake_publish_api)

        with test_app.session_factory() as db:
            record = db.query(PublishRecord).filter(PublishRecord.task_id == task_id).one()
            article = _load_article_for_publish(db, record.article_id)
            account = db.get(Account, record.account_id)
            _detach_record_inputs(db, record, article, account)
            runner = build_publish_runner_for_record(record)

        # account 现已 detached；修复前 _build_api_payload 读 account.platform.code 抛 DetachedInstanceError
        try:
            result = runner(article, account, stop_before_publish=False)
        except DetachedInstanceError as exc:  # pragma: no cover - 仅用于断言信息
            raise AssertionError(f"detached account 触发了 platform 懒加载：{exc}") from exc

        assert result.message == "draft-ok"
        assert captured["platform_code"] == "wechat_mp"
    finally:
        test_app.cleanup()


def test_export_excludes_soft_deleted_accounts(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        deleted_id = test_app.client.post("/api/accounts", json=_create_payload()).json()["id"]
        live_id = test_app.client.post(
            "/api/accounts",
            json=_create_payload(
                display_name="存活号",
                api_credentials={"app_id": "wxLIVEKEEP0001", "app_secret": "live-secret-7b2c"},
            ),
        ).json()["id"]

        assert test_app.client.delete(f"/api/accounts/{deleted_id}").status_code == 204

        resp = test_app.client.post("/api/accounts/export", json={})
        assert resp.status_code == 200, resp.text
        with zipfile.ZipFile(BytesIO(resp.content)) as archive:
            names = archive.namelist()
            assert any(n.startswith(f"accounts/wechat_mp-{live_id}/") for n in names), names
            assert not any(n.startswith(f"accounts/wechat_mp-{deleted_id}/") for n in names), names
    finally:
        test_app.cleanup()
