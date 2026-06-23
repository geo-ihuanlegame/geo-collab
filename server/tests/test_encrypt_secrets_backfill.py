"""回填脚本：DB 行 + 文件，幂等。"""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text as sa_text

from server.app.core import crypto
from server.app.core.config import get_settings
from server.app.modules.accounts import secret_files
from server.scripts import encrypt_secrets


def _set_key(monkeypatch) -> None:
    monkeypatch.setenv("GEO_SECRET_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()
    crypto.get_cipher.cache_clear()


def test_backfill_files_idempotent(monkeypatch, tmp_path):
    _set_key(monkeypatch)
    bs = tmp_path / "browser_states" / "toutiao" / "acc"
    bs.mkdir(parents=True)
    f = bs / "storage_state.json"
    f.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")  # 明文

    changed1 = encrypt_secrets.backfill_files(tmp_path)
    assert changed1 == 1
    assert f.read_bytes().startswith(b"enc:v1:")
    # 再跑一遍：已加密，0 改动（幂等）
    changed2 = encrypt_secrets.backfill_files(tmp_path)
    assert changed2 == 0
    assert secret_files.read_state(f) == {"cookies": [], "origins": []}


@pytest.mark.mysql
def test_backfill_db_idempotent(monkeypatch):
    _set_key(monkeypatch)
    from server.app.modules.system.models import Platform
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            if db.query(Platform).filter(Platform.code == "wechat_mp").first() is None:
                db.add(Platform(code="wechat_mp", name="微信公众号",
                                base_url="https://mp.weixin.qq.com", enabled=True))
                db.commit()
        # 经 API 建号（ORM 已加密），再用原始 SQL 改回明文，模拟迁移前的遗留明文行
        resp = test_app.client.post("/api/accounts", json={
            "platform_code": "wechat_mp",
            "display_name": "回填测试号",
            "api_credentials": {"app_id": "wxbackfill01", "app_secret": "plain-secret"},
        })
        assert resp.status_code == 200, resp.text
        with test_app.session_factory() as db:
            db.execute(
                sa_text("UPDATE accounts SET api_credentials = :c "
                        "WHERE platform_user_id = 'wxbackfill01'"),
                {"c": json.dumps({"app_id": "wxbackfill01", "app_secret": "plain-secret"})},
            )
            db.commit()

        with test_app.session_factory() as db:
            assert encrypt_secrets.backfill_db(db) == 1
        with test_app.session_factory() as db:
            raw = db.execute(sa_text(
                "SELECT api_credentials FROM accounts WHERE platform_user_id = 'wxbackfill01'"
            )).scalar_one()
        assert raw.startswith("enc:v1:")
        assert "plain-secret" not in raw
        # 幂等：再跑 0 改动
        with test_app.session_factory() as db:
            assert encrypt_secrets.backfill_db(db) == 0
    finally:
        test_app.cleanup()
