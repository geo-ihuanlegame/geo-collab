"""EncryptedJSON / EncryptedText 的 bind/result 处理纯单测（无需 DB）。"""

from __future__ import annotations

from cryptography.fernet import Fernet

from server.app.core import crypto
from server.app.core.config import get_settings
from server.app.core.encrypted_types import EncryptedJSON, EncryptedText


def _set_key(monkeypatch) -> None:
    monkeypatch.setenv("GEO_SECRET_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()
    crypto.get_cipher.cache_clear()


def test_encrypted_json_bind_then_result_roundtrip(monkeypatch):
    _set_key(monkeypatch)
    col = EncryptedJSON()
    stored = col.process_bind_param({"app_id": "a", "app_secret": "s"}, dialect=None)
    assert stored.startswith("enc:v1:")
    assert "app_secret" not in stored
    assert col.process_result_value(stored, dialect=None) == {"app_id": "a", "app_secret": "s"}


def test_encrypted_json_none_passthrough(monkeypatch):
    _set_key(monkeypatch)
    col = EncryptedJSON()
    assert col.process_bind_param(None, dialect=None) is None
    assert col.process_result_value(None, dialect=None) is None


def test_encrypted_json_reads_legacy_plaintext(monkeypatch):
    _set_key(monkeypatch)
    col = EncryptedJSON()
    # 迁移前的明文 JSON 文本（无 enc: 前缀）应能直接读出
    assert col.process_result_value('{"app_id": "a"}', dialect=None) == {"app_id": "a"}


def test_encrypted_text_roundtrip(monkeypatch):
    _set_key(monkeypatch)
    col = EncryptedText()
    stored = col.process_bind_param("token-123", dialect=None)
    assert stored.startswith("enc:v1:")
    assert col.process_result_value(stored, dialect=None) == "token-123"
