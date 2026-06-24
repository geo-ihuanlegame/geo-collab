import io
import json
import zipfile

import pytest

from server.app.modules.accounts import secret_files
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
        assert secret_files.read_state(state_file) == {"cookies": [], "origins": []}
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


# ── 导入状态评估单元测试 ───────────────────────────────────────────────────


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

    past = int(time.time()) - 3600  # 1 小时前
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

    future = int(time.time()) + 86400  # 1 天后
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


@pytest.mark.mysql
def test_export_plaintext_import_reencrypts(monkeypatch):
    """导出 ZIP 内 storage_state 是明文，导入后落盘是密文，read_state 仍能读回原值。"""
    from cryptography.fernet import Fernet

    from server.app.core import crypto

    monkeypatch.setenv("GEO_SECRET_KEY", Fernet.generate_key().decode("ascii"))
    from server.app.core.config import get_settings

    get_settings.cache_clear()
    crypto.get_cipher.cache_clear()

    app1 = build_test_app(monkeypatch)
    try:
        # 1) 写带可识别 cookie 值的 state 文件（走 write_state 使落盘为密文）
        account_key = "enc-test"
        state_dir = app1.data_dir / "browser_states" / "toutiao" / account_key
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file = state_dir / "storage_state.json"
        secret_files.write_state(
            state_file,
            {"cookies": [{"name": "s", "value": "marker-cookie"}], "origins": []},
        )

        # 2) 建账号（use_browser=False 直接使用现有 state 文件）
        account_resp = app1.client.post(
            "/api/accounts/toutiao/login",
            json={
                "display_name": "enc-export-test",
                "account_key": account_key,
                "use_browser": False,
                "note": "enc",
            },
        )
        assert account_resp.status_code == 200, account_resp.text
        account = account_resp.json()

        # 3) 导出
        export_resp = app1.client.post(
            "/api/accounts/export", json={"account_ids": [account["id"]]}
        )
        assert export_resp.status_code == 200
        zip_bytes = export_resp.content

        # 断言 ①: ZIP 内 storage_state 是明文，含 marker-cookie
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            entry_name = next(n for n in z.namelist() if n.endswith("storage_state.json"))
            zipped = z.read(entry_name)
        assert b"marker-cookie" in zipped, "ZIP entry should contain plaintext cookie value"
        assert json.loads(zipped)["cookies"][0]["value"] == "marker-cookie"
    finally:
        app1.cleanup()

    # 4) 导入到新 app（同进程、同 GEO_SECRET_KEY）
    app2 = build_test_app(monkeypatch)
    try:
        import_resp = app2.client.post(
            "/api/accounts/import",
            files={"file": ("export.zip", zip_bytes, "application/zip")},
        )
        assert import_resp.status_code == 200, import_resp.text
        body = import_resp.json()
        assert body["imported"] == ["enc-export-test"]

        accounts2 = app2.client.get("/api/accounts").json()
        imported = accounts2[0]

        dest = app2.data_dir / imported["state_path"]
        assert dest.exists()

        # 断言 ②: 落盘文件以 enc:v1: 前缀（密文）
        assert dest.read_bytes().startswith(b"enc:v1:"), "imported file should be encrypted"

        # 断言 ③: 落盘不含 marker-cookie 明文
        assert b"marker-cookie" not in dest.read_bytes(), "encrypted file must not expose cookie"

        # 断言 ④: read_state 能解密读回原值
        assert secret_files.read_state(dest)["cookies"][0]["value"] == "marker-cookie"
    finally:
        app2.cleanup()
        # 恢复 cipher cache 避免污染其他测试
        crypto.get_cipher.cache_clear()
        get_settings.cache_clear()
