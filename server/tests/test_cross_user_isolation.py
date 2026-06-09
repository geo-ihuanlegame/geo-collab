"""跨用户资源隔离（IDOR / 越权 404）契约。

所有归属守卫遵循同一规则：非 admin 访问他人资源一律 404（不泄露其存在），admin 可越权访问：
  - pipelines        `_owned`                    (router.py:39)
  - ai_generation    `_get_owned_scheme`         (scheme_router.py，方案仍按用户私有)
  - accounts         `_verify_account_ownership` (router.py:48，注释明确「404 而非 403」)

例外：**问题池（question pool）改为全员共享**，不再按属主隔离——任意登录用户都能看到 / 改名 /
同步同一批池，新增也共享；唯独「删除」收归 admin（require_admin → 403）。下面的用例覆盖这一新契约。

build_test_app 默认只有一个 admin，现有测试几乎都以这单个 admin 跑，从未真正触发「他人资源」分支。
这里用 create_extra_user 再造一个 operator 来覆盖该越权分支。用 GET / DELETE 等无 body 动作断言，
避免请求体 422 抢在归属守卫 404 之前。
"""

import json as _json
from pathlib import Path

import pytest

from server.tests.utils import build_test_app, create_extra_user


def _make_account_for(client, app, key, name="账号"):
    state_dir = Path(app.data_dir) / "browser_states" / "toutiao" / key
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "storage_state.json").write_text(
        _json.dumps({"cookies": [], "origins": []}), encoding="utf-8"
    )
    r = client.post(
        "/api/accounts/toutiao/login",
        json={"display_name": name, "account_key": key, "use_browser": False},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.mysql
def test_cross_user_pipeline_is_404_admin_bypasses(monkeypatch):
    app = build_test_app(monkeypatch)
    admin = app.client  # 默认 admin
    try:
        _op_id, op = create_extra_user(app, "op_pipe", role="operator")

        admin_pid = admin.post(
            "/api/pipelines", json={"name": "admin的", "type": "distribution"}
        ).json()["id"]

        # operator 看不到 / 删不了 admin 的 pipeline → 404
        assert op.get(f"/api/pipelines/{admin_pid}").status_code == 404
        assert op.delete(f"/api/pipelines/{admin_pid}").status_code == 404

        # 对照 1：operator 能看自己的 pipeline（证明不是所有 GET 都 404）
        op_pid = op.post("/api/pipelines", json={"name": "op的", "type": "distribution"}).json()[
            "id"
        ]
        assert op.get(f"/api/pipelines/{op_pid}").status_code == 200

        # 对照 2：admin 越权可见 operator 的 pipeline → 200
        assert admin.get(f"/api/pipelines/{op_pid}").status_code == 200
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_scheme_private_but_pool_shared(monkeypatch):
    """方案仍按用户私有（越权 404）；问题池改为全员共享（他人也能看到 / 读 question-types）。"""
    from server.app.modules.ai_generation.models import GenerationScheme, QuestionPool
    from server.app.modules.system.models import User

    app = build_test_app(monkeypatch)
    admin = app.client
    try:
        _op_id, op = create_extra_user(app, "op_scheme", role="operator")

        # admin 直建一个问题池 + 方案（ORM 直建，user_id=admin）
        with app.session_factory() as db:
            admin_id = db.query(User).filter(User.username == "testadmin").first().id
            pool = QuestionPool(user_id=admin_id, name="admin池")
            db.add(pool)
            db.flush()
            scheme = GenerationScheme(user_id=admin_id, pool_id=pool.id, name="admin方案")
            db.add(scheme)
            db.commit()
            pool_id, scheme_id = pool.id, scheme.id

        # 方案仍私有：operator 越权 → 404
        assert op.get(f"/api/generation/schemes/{scheme_id}").status_code == 404
        assert op.delete(f"/api/generation/schemes/{scheme_id}").status_code == 404

        # 问题池共享：operator 能读 question-types（200）且在列表里看到 admin 建的池
        assert op.get(f"/api/generation/question-pools/{pool_id}/question-types").status_code == 200
        listed = op.get("/api/generation/question-pools").json()
        assert any(p["id"] == pool_id for p in listed)

        # 对照：admin 自己可见方案
        assert admin.get(f"/api/generation/schemes/{scheme_id}").status_code == 200
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_question_pool_shared_crud_permissions(monkeypatch):
    """共享问题池权限矩阵：全员可建 / 看 / 改名 / 同步；删除仅 admin（operator → 403）。"""
    from server.app.modules.ai_generation.models import QuestionPool
    from server.app.modules.system.models import User

    app = build_test_app(monkeypatch)
    admin = app.client
    try:
        _op_id, op = create_extra_user(app, "op_pool", role="operator")

        # admin 直建一个池
        with app.session_factory() as db:
            admin_id = db.query(User).filter(User.username == "testadmin").first().id
            pool = QuestionPool(user_id=admin_id, name="原名")
            db.add(pool)
            db.commit()
            pool_id = pool.id

        # operator 改名（修改）→ 200
        r = op.patch(f"/api/generation/question-pools/{pool_id}", json={"name": "改后名"})
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "改后名"

        # operator 新建 → 201（全员可建）
        assert op.post("/api/generation/question-pools", json={"name": "op建的"}).status_code == 201

        # operator 删除 → 403（仅 admin）
        assert op.delete(f"/api/generation/question-pools/{pool_id}").status_code == 403

        # admin 删除 → 204，删除后列表不再出现
        assert admin.delete(f"/api/generation/question-pools/{pool_id}").status_code == 204
        listed = admin.get("/api/generation/question-pools").json()
        assert all(p["id"] != pool_id for p in listed)
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_cross_user_account_is_404(monkeypatch):
    app = build_test_app(monkeypatch)
    admin = app.client
    try:
        _op_id, op = create_extra_user(app, "op_acct", role="operator")
        acc_id = _make_account_for(admin, app, "admin-acc", "admin账号")

        # check 路由用 get_current_user + _verify_account_ownership（operator 可调，无 body 必填项）。
        # operator 越权 check admin 的账号 → 404（不泄露其存在）。
        # 注：DELETE 账号是 admin-only（require_admin → 403），属另一套权限契约，这里只验跨用户 404。
        assert (
            op.post(f"/api/accounts/{acc_id}/check", json={"use_browser": False}).status_code == 404
        )
        # 对照：admin（属主）能 check → 200，证明上面的 404 是越权拦截而非账号不存在
        assert (
            admin.post(f"/api/accounts/{acc_id}/check", json={"use_browser": False}).status_code
            == 200
        )
    finally:
        app.cleanup()
