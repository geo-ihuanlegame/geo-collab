"""storage_state 文件加解密助手测试（无需 DB）。"""

from __future__ import annotations

import json

from cryptography.fernet import Fernet

from server.app.core import crypto
from server.app.core.config import get_settings
from server.app.modules.accounts import secret_files


def _set_key(monkeypatch) -> None:
    monkeypatch.setenv("GEO_SECRET_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()
    crypto.get_cipher.cache_clear()


def _clear_keys(monkeypatch) -> None:
    monkeypatch.delenv("GEO_SECRET_KEY", raising=False)
    monkeypatch.delenv("GEO_SECRET_KEYS", raising=False)
    get_settings.cache_clear()
    crypto.get_cipher.cache_clear()


def test_write_then_read_roundtrip(monkeypatch, tmp_path):
    _set_key(monkeypatch)
    state = {"cookies": [{"name": "sid", "value": "secret-cookie"}], "origins": []}
    p = tmp_path / "sub" / "storage_state.json"
    secret_files.write_state(p, state)
    # 磁盘上是密文，不含明文 cookie
    assert b"secret-cookie" not in p.read_bytes()
    assert p.read_bytes().startswith(b"enc:v1:")
    assert secret_files.read_state(p) == state


def test_read_legacy_plaintext(monkeypatch, tmp_path):
    _set_key(monkeypatch)
    state = {"cookies": [], "origins": []}
    p = tmp_path / "storage_state.json"
    p.write_text(json.dumps(state), encoding="utf-8")  # 明文写入（模拟迁移前）
    assert secret_files.read_state(p) == state


def test_passthrough_without_key(monkeypatch, tmp_path):
    _clear_keys(monkeypatch)
    state = {"cookies": [], "origins": []}
    p = tmp_path / "storage_state.json"
    secret_files.write_state(p, state)
    assert p.read_bytes().startswith(b"{")  # 无密钥＝明文落盘
    assert secret_files.read_state(p) == state
