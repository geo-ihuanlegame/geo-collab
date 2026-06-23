"""crypto 核心纯函数测试（无需 DB）。"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from server.app.core import crypto
from server.app.core.config import get_settings


def _set_key(monkeypatch, value: str) -> None:
    monkeypatch.setenv("GEO_SECRET_KEY", value)
    get_settings.cache_clear()
    crypto.get_cipher.cache_clear()


def _clear_keys(monkeypatch) -> None:
    monkeypatch.delenv("GEO_SECRET_KEY", raising=False)
    monkeypatch.delenv("GEO_SECRET_KEYS", raising=False)
    get_settings.cache_clear()
    crypto.get_cipher.cache_clear()


def test_roundtrip_str_with_key(monkeypatch):
    _set_key(monkeypatch, Fernet.generate_key().decode("ascii"))
    token = crypto.encrypt_str("super-secret")
    assert token.startswith("enc:v1:")
    assert "super-secret" not in token
    assert crypto.decrypt_str(token) == "super-secret"


def test_roundtrip_bytes_with_key(monkeypatch):
    _set_key(monkeypatch, Fernet.generate_key().decode("ascii"))
    blob = crypto.encrypt_bytes(b'{"cookies": []}')
    assert blob.startswith(b"enc:v1:")
    assert b"cookies" not in blob
    assert crypto.decrypt_bytes(blob) == b'{"cookies": []}'


def test_null_cipher_passthrough_when_no_key(monkeypatch):
    _clear_keys(monkeypatch)
    assert crypto.encrypt_str("x") == "x"
    assert crypto.decrypt_str("x") == "x"
    assert crypto.encrypt_bytes(b"x") == b"x"


def test_decrypt_legacy_plaintext_passthrough(monkeypatch):
    _set_key(monkeypatch, Fernet.generate_key().decode("ascii"))
    # 无 enc: 前缀 = 遗留明文，原样返回
    assert crypto.decrypt_str('{"app_id": "a"}') == '{"app_id": "a"}'
    assert crypto.decrypt_bytes(b'{"cookies": []}') == b'{"cookies": []}'


def test_decrypt_encrypted_without_key_raises(monkeypatch):
    _set_key(monkeypatch, Fernet.generate_key().decode("ascii"))
    token = crypto.encrypt_str("secret")
    _clear_keys(monkeypatch)
    with pytest.raises(RuntimeError):
        crypto.decrypt_str(token)


def test_multifernet_rotation(monkeypatch):
    k_old = Fernet.generate_key().decode("ascii")
    k_new = Fernet.generate_key().decode("ascii")
    # 用旧钥加密
    _set_key(monkeypatch, k_old)
    token = crypto.encrypt_str("rotate-me")
    # 新钥入队首、旧钥保留 → 仍可解；新写入用新钥
    monkeypatch.setenv("GEO_SECRET_KEYS", f"{k_new},{k_old}")
    monkeypatch.delenv("GEO_SECRET_KEY", raising=False)
    get_settings.cache_clear()
    crypto.get_cipher.cache_clear()
    assert crypto.decrypt_str(token) == "rotate-me"


def test_decrypt_bytes_encrypted_without_key_raises(monkeypatch):
    _set_key(monkeypatch, Fernet.generate_key().decode("ascii"))
    blob = crypto.encrypt_bytes(b"secret")
    _clear_keys(monkeypatch)
    with pytest.raises(RuntimeError):
        crypto.decrypt_bytes(blob)


def test_is_encrypted(monkeypatch):
    _set_key(monkeypatch, Fernet.generate_key().decode("ascii"))
    assert crypto.is_encrypted(crypto.encrypt_str("x")) is True
    assert crypto.is_encrypted("plain") is False
    assert crypto.is_encrypted(crypto.encrypt_bytes(b"x")) is True
    assert crypto.is_encrypted(b"plain") is False
    assert crypto.is_encrypted(bytearray(crypto.encrypt_bytes(b"x"))) is True
    assert crypto.is_encrypted(bytearray(b"plain")) is False
