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


import pytest
from sqlalchemy import text as sa_text


@pytest.mark.mysql
def test_api_credentials_encrypted_at_rest(monkeypatch):
    monkeypatch.setenv("GEO_SECRET_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()
    crypto.get_cipher.cache_clear()
    from server.app.modules.system.models import Platform
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            if db.query(Platform).filter(Platform.code == "wechat_mp").first() is None:
                db.add(Platform(code="wechat_mp", name="微信公众号",
                                base_url="https://mp.weixin.qq.com", enabled=True))
                db.commit()
        # 经真实 API 建号 → 走 ORM 加密写入路径
        resp = test_app.client.post("/api/accounts", json={
            "platform_code": "wechat_mp",
            "display_name": "加密测试号",
            "api_credentials": {"app_id": "wxappid123456", "app_secret": "shh-secret"},
        })
        assert resp.status_code == 200, resp.text
        # API 回包不含 secret 原文，只回尾 4 位
        assert resp.json()["app_secret_tail"] == "cret"
        # 原始 DB cell 是密文
        with test_app.session_factory() as db:
            raw = db.execute(sa_text(
                "SELECT api_credentials FROM accounts WHERE platform_user_id = 'wxappid123456'"
            )).scalar_one()
        assert raw.startswith("enc:v1:")
        assert "shh-secret" not in raw
    finally:
        test_app.cleanup()
