import json
import threading
import zipfile
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient

from server.app.core.security import create_access_token
from server.app.modules.accounts import RemoteBrowserSession
from server.app.modules.accounts.models import Account
from server.app.modules.system.models import User
from server.tests.utils import build_test_app


class FakeDriver:
    code = "toutiao"
    name = "头条号"
    home_url = "https://mp.toutiao.com"
    publish_url = "https://mp.toutiao.com/profile_v4/graphic/publish"

    def detect_logged_in(self, *, url: str, title: str, body: str) -> bool:
        haystack = f"{url}\n{title}\n{body}"
        if any(hint in haystack for hint in ("login", "passport", "sso", "验证码", "扫码", "登录")):
            return False
        return "mp.toutiao.com" in url and ("profile_v4" in url or "头条号" in title)


def install_fake_driver(monkeypatch) -> None:
    monkeypatch.setattr("server.app.modules.accounts.router.all_driver_codes", lambda: ["toutiao"])
    monkeypatch.setattr(
        "server.app.modules.accounts.auth._get_driver", lambda platform_code: FakeDriver()
    )
    monkeypatch.setattr(
        "server.app.modules.accounts.service._get_driver", lambda platform_code: FakeDriver()
    )


def write_storage_state(data_dir, account_key: str = "demo") -> None:
    state_dir = data_dir / "browser_states" / "toutiao" / account_key
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "storage_state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")


def test_start_login_browser_delegates_to_impl(monkeypatch):
    """``_start_login_browser`` resolves the state path and delegates to the impl.

    Thread isolation is no longer this function's job — the async login broker owns
    the event loop now (see test_login_broker.py). This just verifies delegation.
    """
    from server.app.modules.accounts import auth as accounts_auth

    class FakeSession:
        id = "thread-session"
        novnc_url = "http://127.0.0.1:6080/vnc.html"

    captured: dict[str, object] = {}

    def fake_impl(platform_code, account_key, state_path, channel, executable_path, *_a):
        captured["platform_code"] = platform_code
        captured["account_key"] = account_key
        captured["channel"] = channel
        return FakeSession()

    monkeypatch.setattr(accounts_auth, "_start_login_browser_impl", fake_impl)

    session = accounts_auth._start_login_browser("toutiao", "thread-test", "chromium", None)

    assert session.id == "thread-session"
    assert captured["platform_code"] == "toutiao"
    assert captured["account_key"] == "thread-test"
    assert captured["channel"] == "chromium"


def test_worker_start_login_session_uses_existing_thread(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    install_fake_driver(monkeypatch)
    owner_thread = threading.get_ident()

    class FakeSession:
        id = "wk-browser"
        novnc_url = "http://127.0.0.1:6080/vnc.html"

    def fake_start_impl(*_args):
        assert threading.get_ident() == owner_thread
        return FakeSession()

    monkeypatch.setattr(
        "server.app.modules.accounts.auth._start_login_browser_impl", fake_start_impl
    )

    try:
        write_storage_state(test_app.data_dir, "demo")
        account = client.post(
            "/api/accounts/toutiao/login",
            json={"display_name": "worker-start-demo", "account_key": "demo", "use_browser": False},
        ).json()

        from server.app.modules.accounts import auth as accounts
        from server.app.modules.accounts.models import AccountLoginSession

        db = test_app.session_factory()
        try:
            request = AccountLoginSession(
                id="wk-start",
                account_id=account["id"],
                platform_code="toutiao",
                account_key="demo",
                channel="chromium",
                status=accounts.LOGIN_STATUS_STARTING,
            )
            db.add(request)
            db.commit()

            accounts._worker_start_login_session(db, request)
            db.refresh(request)

            assert request.status == accounts.LOGIN_STATUS_ACTIVE
            assert request.browser_session_id == "wk-browser"
            assert request.novnc_url == "http://127.0.0.1:6080/vnc.html"
        finally:
            db.close()
    finally:
        test_app.cleanup()


def test_worker_finish_login_session_reads_state_via_broker(monkeypatch):
    """Worker finish reads the login state through the async broker and tears it down.

    The Playwright handles live in the login broker now, not on the RemoteBrowserSession;
    we stub the broker facade so the test stays free of a real browser/event loop.
    """
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    install_fake_driver(monkeypatch)

    try:
        write_storage_state(test_app.data_dir, "demo")
        account = client.post(
            "/api/accounts/toutiao/login",
            json={
                "display_name": "worker-finish-demo",
                "account_key": "demo",
                "use_browser": False,
            },
        ).json()

        from server.app.modules.accounts import auth as accounts
        from server.app.modules.accounts import browser as browser_sessions
        from server.app.modules.accounts.login_broker import LoginBrowserResult, login_broker
        from server.app.modules.accounts.models import AccountLoginSession

        session = RemoteBrowserSession(
            id="wk-session",
            account_key="demo",
            display_number=99,
            display=":99",
            vnc_port=5900,
            novnc_port=6080,
            novnc_url="http://127.0.0.1:6080/vnc.html",
            log_dir=test_app.data_dir,
        )
        with browser_sessions._sessions_lock:
            browser_sessions._active_sessions[session.id] = session
            browser_sessions._session_keep_alive.add(session.id)

        closed: dict[str, bool] = {}

        def fake_read(session_id, *, detect, state_path):
            assert session_id == "wk-session"
            Path(state_path).parent.mkdir(parents=True, exist_ok=True)
            Path(state_path).write_text(
                '{"cookies":[{"name":"session"}],"origins":[]}', encoding="utf-8"
            )
            logged_in = detect(
                "https://mp.toutiao.com/profile_v4", "Toutiao", "publisher dashboard"
            )
            return LoginBrowserResult(
                logged_in=logged_in, url="https://mp.toutiao.com/profile_v4", title="Toutiao"
            )

        monkeypatch.setattr(login_broker, "owns", lambda sid: sid == "wk-session")
        monkeypatch.setattr(login_broker, "read_login_state", fake_read)
        monkeypatch.setattr(
            login_broker, "close_if_owned", lambda sid: closed.__setitem__(sid, True)
        )

        db = test_app.session_factory()
        try:
            request = AccountLoginSession(
                id="wk-finish",
                account_id=account["id"],
                platform_code="toutiao",
                account_key="demo",
                channel="chromium",
                status=accounts.LOGIN_STATUS_FINISHING,
                browser_session_id="wk-session",
            )
            db.add(request)
            db.commit()

            accounts._worker_finish_login_session(db, request)
            db.refresh(request)

            assert request.status == accounts.LOGIN_STATUS_FINISHED
            assert request.logged_in is True
            assert browser_sessions.get_session("wk-session") is None
            assert closed.get("wk-session") is True
        finally:
            db.close()
    finally:
        test_app.cleanup()


def test_toutiao_login_registers_existing_storage_and_lists_account(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    install_fake_driver(monkeypatch)

    try:
        write_storage_state(test_app.data_dir, "demo")

        response = client.post(
            "/api/accounts/toutiao/login",
            json={
                "display_name": "测试头条号",
                "account_key": "demo",
                "use_browser": False,
                "note": "fixture",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["display_name"] == "测试头条号"
        assert payload["platform_code"] == "toutiao"
        assert payload["status"] == "valid"
        assert payload["state_path"] == "browser_states/users/1/toutiao/demo/storage_state.json"

        list_response = client.get("/api/accounts")
        assert list_response.status_code == 200
        assert [item["id"] for item in list_response.json()] == [payload["id"]]
    finally:
        test_app.cleanup()


def test_account_check_relogin_and_delete(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    install_fake_driver(monkeypatch)

    try:
        write_storage_state(test_app.data_dir, "demo")
        account = client.post(
            "/api/accounts/toutiao/login",
            json={"display_name": "测试头条号", "account_key": "demo", "use_browser": False},
        ).json()

        checked = client.post(f"/api/accounts/{account['id']}/check", json={"use_browser": False})
        assert checked.status_code == 200
        assert checked.json()["status"] == "valid"

        with test_app.session_factory() as db:
            stored = db.get(Account, account["id"])
            assert stored is not None
            stored.state_path = "browser_states/toutiao/demo-missing/storage_state.json"
            db.commit()

        expired = client.post(f"/api/accounts/{account['id']}/check", json={"use_browser": False})
        assert expired.status_code == 200
        assert expired.json()["status"] == "expired"

        write_storage_state(test_app.data_dir, "demo-missing")
        relogged = client.post(
            f"/api/accounts/{account['id']}/relogin", json={"use_browser": False}
        )
        assert relogged.status_code == 200
        assert relogged.json()["status"] == "valid"

        deleted = client.delete(f"/api/accounts/{account['id']}")
        assert deleted.status_code == 204
        assert client.get("/api/accounts").json() == []
    finally:
        test_app.cleanup()


def test_operator_cannot_delete_account_gets_clear_reason(monkeypatch):
    """普通(operator)账号删除媒体矩阵账号：被 require_admin 拦截，返回 403「需要管理员权限」，

    与删文章/删分组的边界一致；账号仍在；admin 仍可删。清晰说明由前端承担（点删除即提示、不发请求）。
    """
    test_app = build_test_app(monkeypatch)
    admin_client = test_app.client
    install_fake_driver(monkeypatch)

    try:
        write_storage_state(test_app.data_dir, "demo")
        account = admin_client.post(
            "/api/accounts/toutiao/login",
            json={"display_name": "测试头条号", "account_key": "demo", "use_browser": False},
        ).json()

        # 造一个 operator，以其身份请求删除
        with test_app.session_factory() as db:
            op = User(
                username="op_del", role="operator", is_active=True, must_change_password=False
            )
            op.set_password("pass1234")
            db.add(op)
            db.commit()
            db.refresh(op)
            token = create_access_token(op.id, op.role)
        op_client = TestClient(test_app.client.app)
        op_client.cookies["access_token"] = token

        resp = op_client.delete(f"/api/accounts/{account['id']}")
        assert resp.status_code == 403
        assert resp.json()["detail"] == "需要管理员权限"

        # 账号未被删除，admin 视角仍可见
        assert len(admin_client.get("/api/accounts").json()) == 1

        # admin 自己仍可正常删除
        assert admin_client.delete(f"/api/accounts/{account['id']}").status_code == 204
    finally:
        test_app.cleanup()


def test_toutiao_login_requires_storage_when_browser_disabled(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    install_fake_driver(monkeypatch)

    try:
        response = client.post(
            "/api/accounts/toutiao/login",
            json={"display_name": "测试头条号", "account_key": "missing", "use_browser": False},
        )

        assert response.status_code == 400
        assert "Storage state not found" in response.json()["detail"]
    finally:
        test_app.cleanup()


def test_toutiao_remote_login_session_creates_unknown_account(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    install_fake_driver(monkeypatch)

    monkeypatch.setattr(
        "server.app.modules.accounts.auth._start_login_browser_via_worker",
        lambda *_args, **_kwargs: "login-session-1",
    )

    try:
        response = client.post(
            "/api/accounts/toutiao/login-session",
            json={"display_name": "remote-demo", "account_key": "remote demo"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["account_key"] == "remote-demo"
        assert payload["platform_code"] == "toutiao"
        assert payload["session_id"] == "login-session-1"
        assert payload["novnc_url"] is None
        assert payload["account"]["display_name"] == "remote-demo"
        assert payload["account"]["status"] == "unknown"
        assert (
            payload["account"]["state_path"]
            == "browser_states/users/1/toutiao/remote-demo/storage_state.json"
        )
    finally:
        test_app.cleanup()


def test_toutiao_remote_login_session_persists_profile_fields(monkeypatch):
    """浏览器平台建号时，表单里的 contact / 分发开关应一并写入账号（A2：复用 /login-session 端点）。"""
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    install_fake_driver(monkeypatch)

    monkeypatch.setattr(
        "server.app.modules.accounts.auth._start_login_browser_via_worker",
        lambda *_args, **_kwargs: "login-session-2",
    )

    try:
        response = client.post(
            "/api/accounts/toutiao/login-session",
            json={
                "display_name": "profile-demo",
                "account_key": "profile-demo",
                "contact": "13800000000",
                "distribution_enabled": False,
                "note": "归属：运营A",
            },
        )

        assert response.status_code == 200
        account = response.json()["account"]
        assert account["contact"] == "13800000000"
        assert account["distribution_enabled"] is False
        assert account["note"] == "归属：运营A"
    finally:
        test_app.cleanup()


def test_finish_remote_login_session_saves_state_and_stops_session(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    install_fake_driver(monkeypatch)

    try:
        write_storage_state(test_app.data_dir, "demo")
        account = client.post(
            "/api/accounts/toutiao/login",
            json={"display_name": "finish-demo", "account_key": "demo", "use_browser": False},
        ).json()

        from server.app.modules.accounts import browser as browser_sessions
        from server.app.modules.accounts.login_broker import LoginBrowserResult, login_broker

        session = RemoteBrowserSession(
            id="finish-session",
            account_key="demo",
            display_number=99,
            display=":99",
            vnc_port=5900,
            novnc_port=6080,
            novnc_url="http://127.0.0.1:6080/vnc.html",
            log_dir=test_app.data_dir,
        )
        with browser_sessions._sessions_lock:
            browser_sessions._active_sessions[session.id] = session
            browser_sessions._session_keep_alive.add(session.id)

        closed: dict[str, bool] = {}

        def fake_read(session_id, *, detect, state_path):
            Path(state_path).parent.mkdir(parents=True, exist_ok=True)
            Path(state_path).write_text(
                '{"cookies":[{"name":"session"}],"origins":[]}', encoding="utf-8"
            )
            logged_in = detect(
                "https://mp.toutiao.com/profile_v4", "Toutiao", "publisher dashboard"
            )
            return LoginBrowserResult(
                logged_in=logged_in, url="https://mp.toutiao.com/profile_v4", title="Toutiao"
            )

        monkeypatch.setattr(login_broker, "owns", lambda sid: sid == "finish-session")
        monkeypatch.setattr(login_broker, "read_login_state", fake_read)
        monkeypatch.setattr(
            login_broker, "close_if_owned", lambda sid: closed.__setitem__(sid, True)
        )

        response = client.post(f"/api/accounts/{account['id']}/login-session/finish-session/finish")

        assert response.status_code == 200
        payload = response.json()
        assert payload["logged_in"] is True
        assert payload["account"]["status"] == "valid"
        assert closed.get("finish-session") is True
        assert browser_sessions.get_session("finish-session") is None
        state_file = (
            test_app.data_dir
            / "browser_states"
            / "users"
            / "1"
            / "toutiao"
            / "demo"
            / "storage_state.json"
        )
        assert json.loads(state_file.read_text(encoding="utf-8"))["cookies"][0]["name"] == "session"
    finally:
        test_app.cleanup()


def test_export_accounts_auth_package_contains_manifest_and_state(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    install_fake_driver(monkeypatch)

    try:
        write_storage_state(test_app.data_dir, "demo")
        account = client.post(
            "/api/accounts/toutiao/login",
            json={"display_name": "export-demo", "account_key": "demo", "use_browser": False},
        ).json()

        response = client.post("/api/accounts/export", json={"account_ids": [account["id"]]})

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            names = set(archive.namelist())
            account_dir = f"accounts/toutiao-{account['id']}"
            assert "manifest.json" in names
            assert f"{account_dir}/account.json" in names
            assert f"{account_dir}/storage_state.json" in names

            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["schema_version"] == 1
            assert manifest["excluded_scopes"] == [
                "articles",
                "assets",
                "publish_tasks",
                "task_logs",
                "database",
            ]
            assert manifest["accounts"][0]["id"] == account["id"]

            account_payload = json.loads(archive.read(f"{account_dir}/account.json"))
            assert account_payload["display_name"] == "export-demo"
            assert (
                archive.read(f"{account_dir}/storage_state.json") == b'{"cookies":[],"origins":[]}'
            )
    finally:
        test_app.cleanup()


def test_unknown_platform_returns_404(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    monkeypatch.setattr("server.app.modules.accounts.router.all_driver_codes", lambda: ["toutiao"])

    try:
        response = client.post(
            "/api/accounts/sohu/login-session",
            json={"display_name": "sohu", "account_key": "sohu"},
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "未知平台"
    finally:
        test_app.cleanup()
