"""共享账号：可见性 / 权限矩阵 / 成员管理 / 删除语义 / AccountRead 派生字段 / admin 批量回填。

对应设计稿 2026-06-17-toutiao-account-dedup-sharing-design.md §6 / §2.6 / §5 触发点 B。
全部走 MySQL（build_test_app）；批量回填 monkeypatch 抽取/检测，免真浏览器。
"""

import pytest

from server.tests.utils import build_test_app, create_extra_user


def _platform(db, code="toutiao", name="头条号"):
    from server.app.modules.accounts.service import get_or_create_platform

    return get_or_create_platform(db, code, name, "https://mp.toutiao.com")


def _make_account(
    app,
    *,
    owner_id,
    name="账号",
    key="acc",
    status="valid",
    platform_user_id=None,
    merged_into=None,
    state_path="browser_states/toutiao/acc/storage_state.json",
    platform_code="toutiao",
    platform_name="头条号",
):
    """直接 ORM 建一个浏览器账号，完全掌控 owner / status / platform_user_id / merged_into。"""
    from server.app.modules.accounts.models import Account

    with app.session_factory() as db:
        platform = _platform(db, platform_code, platform_name)
        account = Account(
            user_id=owner_id,
            platform=platform,
            display_name=name,
            platform_user_id=platform_user_id,
            status=status,
            state_path=state_path,
            merged_into=merged_into,
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        return account.id


def _add_member(app, account_id, user_id, granted_via="manual"):
    from server.app.modules.accounts.models import AccountMember

    with app.session_factory() as db:
        db.add(AccountMember(account_id=account_id, user_id=user_id, granted_via=granted_via))
        db.commit()


# ── 列表可见性 ────────────────────────────────────────────────────────────────


@pytest.mark.mysql
def test_list_visibility_owner_member_nonmember_admin(monkeypatch):
    app = build_test_app(monkeypatch)
    admin_client = app.client
    try:
        owner_id, owner_client = create_extra_user(app, "owner1")
        member_id, member_client = create_extra_user(app, "member1")
        other_id, other_client = create_extra_user(app, "other1")

        acc_id = _make_account(app, owner_id=owner_id, name="共享号", key="shared")
        _add_member(app, acc_id, member_id, granted_via="login_dedup")

        # owner 见
        owner_ids = {a["id"] for a in owner_client.get("/api/accounts").json()}
        assert acc_id in owner_ids

        # 成员见
        member_resp = member_client.get("/api/accounts").json()
        member_ids = {a["id"] for a in member_resp}
        assert acc_id in member_ids

        # 非成员不见
        other_ids = {a["id"] for a in other_client.get("/api/accounts").json()}
        assert acc_id not in other_ids

        # admin 全量见
        admin_ids = {a["id"] for a in admin_client.get("/api/accounts").json()}
        assert acc_id in admin_ids
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_list_excludes_merged_into_rows(monkeypatch):
    app = build_test_app(monkeypatch)
    admin_client = app.client
    try:
        owner_id, owner_client = create_extra_user(app, "owner2")
        canonical = _make_account(
            app, owner_id=owner_id, name="canonical", key="c", platform_user_id="12345678"
        )
        merged = _make_account(
            app, owner_id=owner_id, name="merged", key="m", merged_into=canonical
        )

        # owner 列表不含被并入行
        owner_ids = {a["id"] for a in owner_client.get("/api/accounts").json()}
        assert canonical in owner_ids
        assert merged not in owner_ids

        # admin 也不见被并入行
        admin_ids = {a["id"] for a in admin_client.get("/api/accounts").json()}
        assert merged not in admin_ids
        assert canonical in admin_ids
    finally:
        app.cleanup()


# ── 详情可见性 ────────────────────────────────────────────────────────────────


@pytest.mark.mysql
def test_detail_use_check(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        owner_id, owner_client = create_extra_user(app, "owner3")
        member_id, member_client = create_extra_user(app, "member3")
        other_id, other_client = create_extra_user(app, "other3")

        acc_id = _make_account(app, owner_id=owner_id, name="d", key="d")
        _add_member(app, acc_id, member_id)

        assert owner_client.get(f"/api/accounts/{acc_id}").status_code == 200
        assert member_client.get(f"/api/accounts/{acc_id}").status_code == 200
        # 非成员 → 404（不泄露存在）
        assert other_client.get(f"/api/accounts/{acc_id}").status_code == 404
        # admin → 200
        assert app.client.get(f"/api/accounts/{acc_id}").status_code == 200
    finally:
        app.cleanup()


# ── AccountRead 派生字段 ──────────────────────────────────────────────────────


@pytest.mark.mysql
def test_account_read_fields_owner_member_admin(monkeypatch):
    app = build_test_app(monkeypatch)
    admin_client = app.client
    try:
        owner_id, owner_client = create_extra_user(app, "alice")
        member_id, member_client = create_extra_user(app, "bob")

        # 浏览器平台 + platform_user_id 非空 → identity_known True
        acc_id = _make_account(
            app, owner_id=owner_id, name="字段号", key="f", platform_user_id="87654321"
        )
        _add_member(app, acc_id, member_id)

        owner_view = owner_client.get(f"/api/accounts/{acc_id}").json()
        assert owner_view["owner_name"] == "alice"
        assert owner_view["member_count"] == 1
        assert owner_view["can_manage"] is True
        assert owner_view["identity_known"] is True

        member_view = member_client.get(f"/api/accounts/{acc_id}").json()
        assert member_view["owner_name"] == "alice"
        assert member_view["member_count"] == 1
        assert member_view["can_manage"] is False  # 成员不可管理
        assert member_view["identity_known"] is True

        admin_view = admin_client.get(f"/api/accounts/{acc_id}").json()
        assert admin_view["can_manage"] is True  # admin 可管理

        # platform_user_id 为 NULL → identity_known False（身份未知徽标）
        unknown_id = _make_account(
            app, owner_id=owner_id, name="未知号", key="u", platform_user_id=None
        )
        unknown_view = owner_client.get(f"/api/accounts/{unknown_id}").json()
        assert unknown_view["identity_known"] is False
        assert unknown_view["member_count"] == 0
    finally:
        app.cleanup()


# ── 成员能在任务 / distribute 用共享账号 ──────────────────────────────────────


@pytest.mark.mysql
def test_member_can_use_shared_account_in_task(monkeypatch):
    from server.app.modules.tasks.schemas import TaskAccountInput, TaskCreate
    from server.app.modules.tasks.service import create_task

    app = build_test_app(monkeypatch)
    try:
        owner_id, _ = create_extra_user(app, "owner4")
        member_id, member_client = create_extra_user(app, "member4")

        acc_id = _make_account(app, owner_id=owner_id, name="任务号", key="t", status="valid")
        _add_member(app, acc_id, member_id)

        # 成员建文章（自己的）
        r = member_client.post(
            "/api/articles",
            json={
                "title": "成员文章",
                "content_json": {"type": "doc", "content": []},
                "content_html": "<p>x</p>",
                "plain_text": "x",
                "word_count": 1,
                "status": "ready",
            },
        )
        article_id = r.json()["id"]
        # 审核通过
        with app.session_factory() as db:
            from server.app.modules.articles.models import Article

            art = db.get(Article, article_id)
            art.review_status = "approved"
            db.commit()

        # 成员用共享账号建任务（operator role） → 不应抛 AccountError
        with app.session_factory() as db:
            tc = TaskCreate(
                name="成员任务",
                task_type="single",
                article_id=article_id,
                platform_code="toutiao",
                accounts=[TaskAccountInput(account_id=acc_id, sort_order=0)],
                stop_before_publish=False,
            )
            task = create_task(db, member_id, tc, role="operator")
            db.commit()
            assert task.id is not None

        # 非成员用同一账号建任务 → AccountError（账号对非成员不可见即 not found）。
        # other 用自己的文章以隔离权限点（确保失败来自账号校验而非文章归属）。
        _, other_client = create_extra_user(app, "other4b")
        with app.session_factory() as db:
            from server.app.modules.articles.models import Article
            from server.app.shared.errors import AccountError

            r2 = other_client.post(
                "/api/articles",
                json={
                    "title": "外人文章",
                    "content_json": {"type": "doc", "content": []},
                    "content_html": "<p>y</p>",
                    "plain_text": "y",
                    "word_count": 1,
                    "status": "ready",
                },
            )
            other_article = r2.json()["id"]
            art2 = db.get(Article, other_article)
            art2.review_status = "approved"
            other_uid = art2.user_id
            db.commit()
            tc2 = TaskCreate(
                name="外人任务",
                task_type="single",
                article_id=other_article,
                platform_code="toutiao",
                accounts=[TaskAccountInput(account_id=acc_id, sort_order=0)],
                stop_before_publish=False,
            )
            with pytest.raises(AccountError):
                create_task(db, other_uid, tc2, role="operator")
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_member_account_visible_in_distribute_resolution(monkeypatch):
    from server.app.modules.pipelines.nodes.distribute_node import (
        resolve_distribution_accounts,
    )

    app = build_test_app(monkeypatch)
    try:
        owner_id, _ = create_extra_user(app, "owner5")
        member_id, _ = create_extra_user(app, "member5")
        other_id, _ = create_extra_user(app, "other5")

        acc_id = _make_account(app, owner_id=owner_id, name="派号", key="dist", status="valid")
        _add_member(app, acc_id, member_id)

        selection = {"platforms": ["toutiao"], "extra_account_ids": [], "excluded_account_ids": []}
        with app.session_factory() as db:
            # 成员能解析到共享账号
            member_groups = resolve_distribution_accounts(
                db, selection, user_id=member_id, role="operator"
            )
            member_ids = {aid for _, ids in member_groups for aid in ids}
            assert acc_id in member_ids

            # 非成员解析不到
            other_groups = resolve_distribution_accounts(
                db, selection, user_id=other_id, role="operator"
            )
            other_ids = {aid for _, ids in other_groups for aid in ids}
            assert acc_id not in other_ids

            # admin 解析到全部
            admin_groups = resolve_distribution_accounts(db, selection, user_id=None, role="admin")
            admin_ids = {aid for _, ids in admin_groups for aid in ids}
            assert acc_id in admin_ids
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_distribute_excludes_merged_into(monkeypatch):
    from server.app.modules.pipelines.nodes.distribute_node import (
        resolve_distribution_accounts,
    )

    app = build_test_app(monkeypatch)
    try:
        owner_id, _ = create_extra_user(app, "owner6")
        canonical = _make_account(
            app,
            owner_id=owner_id,
            name="canon",
            key="c6",
            status="valid",
            platform_user_id="11111111",
        )
        merged = _make_account(
            app,
            owner_id=owner_id,
            name="merged",
            key="m6",
            status="valid",
            merged_into=canonical,
        )
        selection = {"platforms": ["toutiao"], "extra_account_ids": [], "excluded_account_ids": []}
        with app.session_factory() as db:
            groups = resolve_distribution_accounts(db, selection, user_id=None, role="admin")
            ids = {aid for _, ids in groups for aid in ids}
            assert canonical in ids
            assert merged not in ids
    finally:
        app.cleanup()


# ── 管理 gating：成员不可改名 / 删除 / 改字段 / 移除成员 ─────────────────────


@pytest.mark.mysql
def test_member_cannot_manage(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        owner_id, owner_client = create_extra_user(app, "owner7")
        member_id, member_client = create_extra_user(app, "member7")

        acc_id = _make_account(app, owner_id=owner_id, name="管理号", key="mg")
        _add_member(app, acc_id, member_id)

        # 成员改名 / 改字段 → 403
        assert (
            member_client.patch(
                f"/api/accounts/{acc_id}", json={"display_name": "改名"}
            ).status_code
            == 403
        )
        assert (
            member_client.patch(
                f"/api/accounts/{acc_id}", json={"distribution_enabled": False}
            ).status_code
            == 403
        )

        # 成员删除 → 403（删除已收归 admin，require_admin 先拦）
        assert member_client.delete(f"/api/accounts/{acc_id}").status_code == 403

        # 成员看成员列表 → 403
        assert member_client.get(f"/api/accounts/{acc_id}/members").status_code == 403

        # 成员移除成员 → 403
        assert (
            member_client.delete(f"/api/accounts/{acc_id}/members/{member_id}").status_code == 403
        )

        # owner 改名 → 200
        assert (
            owner_client.patch(
                f"/api/accounts/{acc_id}", json={"display_name": "owner改名"}
            ).status_code
            == 200
        )
    finally:
        app.cleanup()


# ── 成员管理端点：列表 / 移除 / 幂等 ──────────────────────────────────────────


@pytest.mark.mysql
def test_members_list_and_remove(monkeypatch):
    app = build_test_app(monkeypatch)
    admin_client = app.client
    try:
        owner_id, owner_client = create_extra_user(app, "owner8")
        member_id, _ = create_extra_user(app, "member8")

        acc_id = _make_account(app, owner_id=owner_id, name="成员号", key="ml")
        _add_member(app, acc_id, member_id, granted_via="login_dedup")

        # owner 看成员列表：含 owner 行 + member 行
        rows = owner_client.get(f"/api/accounts/{acc_id}/members").json()
        assert len(rows) == 2
        owner_row = next(r for r in rows if r["is_owner"])
        member_row = next(r for r in rows if not r["is_owner"])
        assert owner_row["user_id"] == owner_id
        assert owner_row["username"] == "owner8"
        assert owner_row["granted_via"] is None
        assert member_row["user_id"] == member_id
        assert member_row["granted_via"] == "login_dedup"

        # owner 移除成员 → 204
        assert owner_client.delete(f"/api/accounts/{acc_id}/members/{member_id}").status_code == 204
        rows_after = owner_client.get(f"/api/accounts/{acc_id}/members").json()
        assert len(rows_after) == 1  # 只剩 owner

        # 幂等：再移除同一成员 → 仍 204
        assert owner_client.delete(f"/api/accounts/{acc_id}/members/{member_id}").status_code == 204

        # admin 也能看 / 移除
        assert admin_client.get(f"/api/accounts/{acc_id}/members").status_code == 200
    finally:
        app.cleanup()


# ── 删除语义：清成员 + 释放槽位 ──────────────────────────────────────────────


@pytest.mark.mysql
def test_delete_clears_members_and_releases_slot(monkeypatch):
    from server.app.modules.accounts.models import Account, AccountMember

    app = build_test_app(monkeypatch)
    admin_client = app.client
    try:
        owner_id, _ = create_extra_user(app, "owner9")
        member_id, _ = create_extra_user(app, "member9")

        acc_id = _make_account(
            app, owner_id=owner_id, name="删除号", key="del", platform_user_id="99999999"
        )
        _add_member(app, acc_id, member_id)

        # admin 软删
        assert admin_client.delete(f"/api/accounts/{acc_id}").status_code == 204

        with app.session_factory() as db:
            acc = db.get(Account, acc_id)
            assert acc.is_deleted is True
            assert acc.platform_user_id is None  # 释放身份槽位
            members = db.query(AccountMember).filter(AccountMember.account_id == acc_id).all()
            assert members == []  # 成员行已清空
    finally:
        app.cleanup()


# ── admin 批量回填 ────────────────────────────────────────────────────────────


@pytest.mark.mysql
def test_backfill_identity_counts(monkeypatch):
    app = build_test_app(monkeypatch)
    admin_client = app.client
    try:
        owner_id, _ = create_extra_user(app, "owner10")

        # 账号 A：将抽取出新 X（无既有 canonical）→ backfilled
        acc_a = _make_account(
            app,
            owner_id=owner_id,
            name="A",
            key="ba",
            status="valid",
            platform_user_id=None,
            state_path="browser_states/toutiao/ba/storage_state.json",
        )
        # 账号 C：已是 canonical（platform_user_id 已写）→ 不在候选（platform_user_id 非空被过滤）。
        # 仅作为 B 的合并落点存在，变量本身无需引用。
        _make_account(
            app,
            owner_id=owner_id,
            name="C",
            key="bc",
            status="valid",
            platform_user_id="20000001",
        )
        # 账号 B：将抽取出 = canonical 的 X → merged
        acc_b = _make_account(
            app,
            owner_id=owner_id,
            name="B",
            key="bb",
            status="valid",
            platform_user_id=None,
            state_path="browser_states/toutiao/bb/storage_state.json",
        )
        # 账号 D：抽取为空 → still_unknown
        acc_d = _make_account(
            app,
            owner_id=owner_id,
            name="D",
            key="bd",
            status="valid",
            platform_user_id=None,
            state_path="browser_states/toutiao/bd/storage_state.json",
        )

        # monkeypatch check_account 的浏览器层：按账号 state_path 决定抽取结果，避免真浏览器。
        extract_map = {acc_a: "30000001", acc_b: "20000001", acc_d: None}

        def fake_check_in_browser(driver, abs_state_path, payload):
            # abs_state_path 形如 .../browser_states/toutiao/<key>/storage_state.json
            key = abs_state_path.parent.name
            keymap = {"ba": acc_a, "bb": acc_b, "bd": acc_d}
            account_id = keymap.get(key)
            return True, extract_map.get(account_id)

        monkeypatch.setattr(
            "server.app.modules.accounts.auth._check_account_in_browser",
            fake_check_in_browser,
        )
        # use_browser 路径要求 state 文件存在 + 抢 profile 锁；造文件 + 桩掉锁。
        from pathlib import Path

        for key in ("ba", "bb", "bd"):
            p = Path(app.data_dir) / "browser_states" / "toutiao" / key
            p.mkdir(parents=True, exist_ok=True)
            (p / "storage_state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
        # check_account 内部局部 import 自 accounts.browser，须打桩该模块上的名字。
        monkeypatch.setattr(
            "server.app.modules.accounts.browser.try_acquire_profile_lock",
            lambda *a, **k: True,
        )
        monkeypatch.setattr(
            "server.app.modules.accounts.browser.release_profile_lock",
            lambda *a, **k: None,
        )
        # 驱动用假驱动（home_url 等）
        from server.tests.test_accounts_api import FakeDriver

        monkeypatch.setattr("server.app.modules.accounts.auth._get_driver", lambda pc: FakeDriver())

        resp = admin_client.post("/api/accounts/backfill-identity")
        assert resp.status_code == 200, resp.text
        summary = resp.json()
        assert summary["processed"] == 3  # A / B / D（C 已有 ID 被过滤）
        assert summary["backfilled"] == 1  # A 升 canonical
        assert summary["merged"] == 1  # B 并入 C
        assert summary["still_unknown"] == 1  # D 抽取空
        assert summary["conflicts"] == 0
        assert summary["failed"] == 0
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_backfill_identity_non_admin_403(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        _, operator_client = create_extra_user(app, "op_backfill")
        assert operator_client.post("/api/accounts/backfill-identity").status_code == 403
    finally:
        app.cleanup()
