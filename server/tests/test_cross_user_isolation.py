"""跨用户资源隔离（IDOR / 越权 404）契约。

所有归属守卫遵循同一规则：非 admin 访问他人资源一律 404（不泄露其存在），admin 可越权访问：
  - pipelines        `_owned`                    (router.py:39)
  - ai_generation    `_get_owned_pool` / `_get_owned_scheme`  (scheme_router.py:50/57)
  - accounts         `_verify_account_ownership` (router.py:48，注释明确「404 而非 403」)

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
def test_cross_user_scheme_and_pool_are_404(monkeypatch):
    from server.app.modules.ai_generation.models import GenerationScheme, QuestionPool
    from server.app.modules.system.models import User

    app = build_test_app(monkeypatch)
    admin = app.client
    try:
        _op_id, op = create_extra_user(app, "op_scheme", role="operator")

        # admin 拥有一个问题池 + 方案（ORM 直建，归属 admin）
        with app.session_factory() as db:
            admin_id = db.query(User).filter(User.username == "testadmin").first().id
            pool = QuestionPool(user_id=admin_id, name="admin池")
            db.add(pool)
            db.flush()
            scheme = GenerationScheme(user_id=admin_id, pool_id=pool.id, name="admin方案")
            db.add(scheme)
            db.commit()
            pool_id, scheme_id = pool.id, scheme.id

        # operator 越权 → 404（_get_owned_pool / _get_owned_scheme 在序列化前先拦）
        assert op.get(f"/api/generation/question-pools/{pool_id}/question-types").status_code == 404
        assert op.get(f"/api/generation/schemes/{scheme_id}").status_code == 404
        assert op.delete(f"/api/generation/schemes/{scheme_id}").status_code == 404

        # 对照：admin 自己可见
        assert (
            admin.get(f"/api/generation/question-pools/{pool_id}/question-types").status_code == 200
        )
        assert admin.get(f"/api/generation/schemes/{scheme_id}").status_code == 200
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
