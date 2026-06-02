import json
import threading
import zipfile
from io import BytesIO

from server.app.modules.accounts import RemoteBrowserSession
from server.app.modules.accounts.models import Account
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


def test_start_login_browser_runs_impl_in_plain_thread(monkeypatch):
    from server.app.modules.accounts import auth as accounts_auth

    caller_thread = threading.get_ident()
    seen: dict[str, int] = {}

    class FakeSession:
        id = "thread-session"
        novnc_url = "http://127.0.0.1:6080/vnc.html"

    def fake_impl(*_args):
        seen["thread"] = threading.get_ident()
        return FakeSession()

    monkeypatch.setattr(accounts_auth, "_start_login_browser_impl", fake_impl)

    session = accounts_auth._start_login_browser("toutiao", "thread-test", "chromium", None)

    assert session.id == "thread-session"
    assert seen["thread"] != caller_thread


def test_login_page_loader_runs_in_background(monkeypatch):
    test_app = build_test_app(monkeypatch)

    started = threading.Event()
    release = threading.Event()

    class FakePage:
        def goto(self, *_args, **_kwargs):
            started.set()
            release.wait(timeout=2)

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

    try:
        from server.app.modules.accounts import auth as accounts
        from server.app.modules.accounts import browser as browser_sessions

        session = RemoteBrowserSession(
            id="loader-session",
            account_key="demo",
            display_number=99,
            display=":99",
            vnc_port=5900,
            novnc_port=6080,
            novnc_url="http://127.0.0.1:6080/vnc.html",
            log_dir=test_app.data_dir,
            page=FakePage(),
        )
        with browser_sessions._sessions_lock:
            browser_sessions._active_sessions[session.id] = session

        accounts._start_login_page_loader(session.id, "toutiao", "demo", "https://mp.toutiao.com")

        assert started.wait(timeout=1)
        assert session.operation_lock.acquire(blocking=False) is False
        release.set()
    finally:
        release.set()
        test_app.cleanup()


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


def test_worker_finish_login_session_uses_existing_thread(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    install_fake_driver(monkeypatch)
    owner_thread = threading.get_ident()

    class FakeLocator:
        def inner_text(self, timeout=None):
            assert threading.get_ident() == owner_thread
            return "publisher dashboard"

    class FakePage:
        url = "https://mp.toutiao.com/profile_v4"

        def wait_for_load_state(self, *_args, **_kwargs):
            assert threading.get_ident() == owner_thread
            return None

        def title(self):
            assert threading.get_ident() == owner_thread
            return "Toutiao"

        def locator(self, _selector):
            assert threading.get_ident() == owner_thread
            return FakeLocator()

    class FakeContext:
        def __init__(self, page):
            self.pages = [page]

        def storage_state(self, path):
            assert threading.get_ident() == owner_thread
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"cookies":[{"name":"session"}],"origins":[]}')

        def close(self):
            assert threading.get_ident() == owner_thread

    class FakePlaywright:
        def stop(self):
            assert threading.get_ident() == owner_thread

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
        from server.app.modules.accounts.models import AccountLoginSession

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

            page = FakePage()
            session = RemoteBrowserSession(
                id="wk-session",
                account_key="demo",
                display_number=99,
                display=":99",
                vnc_port=5900,
                novnc_port=6080,
                novnc_url="http://127.0.0.1:6080/vnc.html",
                log_dir=test_app.data_dir,
                playwright=FakePlaywright(),
                browser_context=FakeContext(page),
                page=page,
            )
            with browser_sessions._sessions_lock:
                browser_sessions._active_sessions[session.id] = session
                browser_sessions._session_keep_alive.add(session.id)

            accounts._worker_finish_login_session(db, request)
            db.refresh(request)

            assert request.status == accounts.LOGIN_STATUS_FINISHED
            assert request.logged_in is True
            assert browser_sessions.get_session("wk-session") is None
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


def test_finish_remote_login_session_saves_state_and_stops_session(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    install_fake_driver(monkeypatch)

    class FakeLocator:
        def inner_text(self, timeout=None):
            return "publisher dashboard"

    class FakePage:
        url = "https://mp.toutiao.com/profile_v4"

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def title(self):
            return "Toutiao"

        def locator(self, _selector):
            return FakeLocator()

    class FakeContext:
        def __init__(self, page):
            self.pages = [page]
            self.closed = False

        def storage_state(self, path):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"cookies":[{"name":"session"}],"origins":[]}')

        def close(self):
            self.closed = True

    class FakePlaywright:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    try:
        write_storage_state(test_app.data_dir, "demo")
        account = client.post(
            "/api/accounts/toutiao/login",
            json={"display_name": "finish-demo", "account_key": "demo", "use_browser": False},
        ).json()

        from server.app.modules.accounts import browser as browser_sessions

        page = FakePage()
        context = FakeContext(page)
        playwright = FakePlaywright()
        session = RemoteBrowserSession(
            id="finish-session",
            account_key="demo",
            display_number=99,
            display=":99",
            vnc_port=5900,
            novnc_port=6080,
            novnc_url="http://127.0.0.1:6080/vnc.html",
            log_dir=test_app.data_dir,
            playwright=playwright,
            browser_context=context,
            page=page,
        )
        with browser_sessions._sessions_lock:
            browser_sessions._active_sessions[session.id] = session
            browser_sessions._session_keep_alive.add(session.id)

        response = client.post(f"/api/accounts/{account['id']}/login-session/finish-session/finish")

        assert response.status_code == 200
        payload = response.json()
        assert payload["logged_in"] is True
        assert payload["account"]["status"] == "valid"
        assert context.closed is True
        assert playwright.stopped is True
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
