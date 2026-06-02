import io
import json
import zipfile

from server.tests.utils import build_test_app


def _contains_any(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def _write_storage_state(data_dir, account_key: str = "demo") -> str:
    state_dir = data_dir / "browser_states" / "toutiao" / account_key
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "storage_state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    return f"browser_states/toutiao/{account_key}/storage_state.json"


def test_export_import_round_trip(monkeypatch):
    app1 = build_test_app(monkeypatch)
    try:
        _write_storage_state(app1.data_dir, "demo")
        account = app1.client.post(
            "/api/accounts/toutiao/login",
            json={
                "display_name": "round-trip-test",
                "account_key": "demo",
                "use_browser": False,
                "note": "rt",
            },
        ).json()

        export_resp = app1.client.post(
            "/api/accounts/export", json={"account_ids": [account["id"]]}
        )
        assert export_resp.status_code == 200
        zip_bytes = export_resp.content
    finally:
        app1.cleanup()

    app2 = build_test_app(monkeypatch)
    try:
        result = app2.client.post(
            "/api/accounts/import", files={"file": ("export.zip", zip_bytes, "application/zip")}
        )
        assert result.status_code == 200
        body = result.json()
        assert body["imported"] == ["round-trip-test"]
        assert body["skipped"] == []

        accounts = app2.client.get("/api/accounts").json()
        assert len(accounts) == 1
        imported = accounts[0]
        assert imported["display_name"] == "round-trip-test"
        assert imported["platform_code"] == "toutiao"
        assert imported["status"] == "expired"
        assert imported["note"] == "rt"
        assert "demo" in imported["state_path"]

        state_file = app2.data_dir / imported["state_path"]
        assert state_file.exists()
        assert state_file.read_bytes() == b'{"cookies":[],"origins":[]}'
    finally:
        app2.cleanup()


def test_import_non_zip_returns_400(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        response = test_app.client.post(
            "/api/accounts/import",
            files={"file": ("export.zip", b"not a zip file", "application/zip")},
        )
        assert response.status_code == 400
        assert _contains_any(response.json()["detail"], "Invalid ZIP", "无效的 ZIP")
    finally:
        test_app.cleanup()


def test_import_path_traversal_returns_400(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "app_version": "0.1.0",
                        "exported_at": "2025-01-01T00:00:00",
                        "excluded_scopes": [],
                        "accounts": [],
                    }
                ),
            )
            archive.writestr("../../etc/passwd", "malicious content")
            archive.writestr("accounts/toutiao-1/storage_state.json", "{}")
            archive.writestr("accounts/toutiao-1/account.json", "{}")
        zip_bytes = buf.getvalue()

        response = test_app.client.post(
            "/api/accounts/import",
            files={"file": ("bad.zip", zip_bytes, "application/zip")},
        )
        assert response.status_code == 400
        content = response.json()["detail"]
        assert _contains_any(
            content, "Invalid ZIP entry path", "无效的 ZIP 条目路径", "无效的授权包"
        )
    finally:
        test_app.cleanup()


def test_import_exceeds_max_zip_bytes_returns_413(monkeypatch):
    monkeypatch.setattr("server.app.core.config.MAX_ZIP_BYTES", 100)
    test_app = build_test_app(monkeypatch)
    try:
        padding = b"x" * 200
        response = test_app.client.post(
            "/api/accounts/import",
            files={"file": ("big.zip", padding, "application/zip")},
        )
        assert response.status_code == 413
        assert _contains_any(response.json()["detail"], "limit", "限制")
    finally:
        test_app.cleanup()


def test_import_oversized_entry_returns_400(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "app_version": "0.1.0",
                        "exported_at": "2025-01-01T00:00:00",
                        "excluded_scopes": [],
                        "accounts": [],
                    }
                ),
            )
            archive.writestr("accounts/toutiao-1/account.json", "{}")
            archive.writestr("accounts/toutiao-1/storage_state.json", "x" * 3 * 1024 * 1024)
        zip_bytes = buf.getvalue()

        response = test_app.client.post(
            "/api/accounts/import",
            files={"file": ("oversized.zip", zip_bytes, "application/zip")},
        )
        assert response.status_code == 400
        assert _contains_any(response.json()["detail"], "ZIP entry too large", "ZIP 条目过大")
    finally:
        test_app.cleanup()


# ── _assess_imported_status 单元测试 ─────────────────────────────────────────


def test_assess_imported_status_empty_cookies(tmp_path):
    """cookies 为空数组 → expired。"""
    from server.app.modules.accounts.auth import _assess_imported_status

    state_file = tmp_path / "storage_state.json"
    state_file.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    assert _assess_imported_status(state_file) == "expired"


def test_assess_imported_status_all_expired(tmp_path):
    """所有 cookies 的 expires 均在过去 → expired。"""
    import time

    from server.app.modules.accounts.auth import _assess_imported_status

    past = int(time.time()) - 3600  # 1 hour ago
    state_file = tmp_path / "storage_state.json"
    state_file.write_text(
        json.dumps({"cookies": [{"name": "sid", "value": "x", "expires": past}], "origins": []}),
        encoding="utf-8",
    )
    assert _assess_imported_status(state_file) == "expired"


def test_assess_imported_status_session_cookie(tmp_path):
    """expires == -1 的 session cookie → valid（无论有没有其他 cookie）。"""
    from server.app.modules.accounts.auth import _assess_imported_status

    state_file = tmp_path / "storage_state.json"
    state_file.write_text(
        json.dumps({"cookies": [{"name": "sess", "value": "abc", "expires": -1}], "origins": []}),
        encoding="utf-8",
    )
    assert _assess_imported_status(state_file) == "valid"


def test_assess_imported_status_future_cookie(tmp_path):
    """至少一个 cookie expires 在未来 → valid。"""
    import time

    from server.app.modules.accounts.auth import _assess_imported_status

    future = int(time.time()) + 86400  # 1 day from now
    state_file = tmp_path / "storage_state.json"
    state_file.write_text(
        json.dumps({"cookies": [{"name": "tok", "value": "y", "expires": future}], "origins": []}),
        encoding="utf-8",
    )
    assert _assess_imported_status(state_file) == "valid"


def test_assess_imported_status_invalid_json(tmp_path):
    """文件内容不是有效 JSON → unknown。"""
    from server.app.modules.accounts.auth import _assess_imported_status

    state_file = tmp_path / "storage_state.json"
    state_file.write_text("not valid json {{{{", encoding="utf-8")
    assert _assess_imported_status(state_file) == "unknown"
