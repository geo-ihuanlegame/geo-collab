from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, text

from alembic import command
from server.app.core.config import get_settings
from server.tests.utils import get_test_database_url, reset_test_database


def _data_dir() -> Path:
    return Path(tempfile.gettempdir()) / "geo-test-data" / uuid.uuid4().hex


def _seed_user(conn, username: str) -> int:
    conn.execute(
        text(
            "INSERT INTO users (username, password_hash, role, is_active, "
            "must_change_password, solo_mode, created_at) "
            "VALUES (:u, 'x', 'admin', 1, 0, 0, NOW())"
        ),
        {"u": username},
    )
    return conn.execute(text("SELECT id FROM users WHERE username=:u"), {"u": username}).scalar()


def _wechat_platform_id(conn) -> int:
    return conn.execute(text("SELECT id FROM platforms WHERE code='wechat_mp'")).scalar()


@pytest.mark.mysql
def test_migration_0045_cleans_dead_rows_and_swaps_constraint(monkeypatch):
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(get_test_database_url(), pool_pre_ping=True)
    reset_test_database(engine, create_schema=False)
    monkeypatch.setenv("GEO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("GEO_DATABASE_URL", get_test_database_url())
    get_settings.cache_clear()

    try:
        cfg = AlembicConfig("alembic.ini")
        command.upgrade(cfg, "0044")

        with engine.begin() as conn:
            uid = _seed_user(conn, "mig-u1")
            pid = _wechat_platform_id(conn)
            conn.execute(
                text(
                    "INSERT INTO accounts (user_id, platform_id, display_name, platform_user_id, "
                    "status, is_deleted, deleted_at, api_credentials, api_token_cache, "
                    "distribution_enabled, created_at, updated_at) VALUES "
                    "(:uid, :pid, 'dead', 'wxDEAD', 'unknown', 1, NOW(), :creds, :tok, 1, "
                    "NOW(), NOW())"
                ),
                {
                    "uid": uid,
                    "pid": pid,
                    "creds": json.dumps({"app_id": "wxDEAD", "app_secret": "sek"}),
                    "tok": json.dumps({"access_token": "t", "expires_at": 1}),
                },
            )

        command.upgrade(cfg, "0045")

        with engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT platform_user_id, api_token_cache, api_credentials "
                        "FROM accounts WHERE display_name='dead'"
                    )
                )
                .mappings()
                .one()
            )
            assert row["platform_user_id"] is None
            assert row["api_token_cache"] is None
            creds = json.loads(row["api_credentials"])
            assert "app_secret" not in creds
            assert creds["app_id"] == "wxDEAD"

            idx = (
                conn.execute(
                    text("SHOW INDEX FROM accounts WHERE Key_name='uq_accounts_platform_user'")
                )
                .mappings()
                .all()
            )
            cols = {r["Column_name"] for r in idx}
            assert cols == {"platform_id", "platform_user_id"}
    finally:
        reset_test_database(engine, create_schema=False)
        engine.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)
        get_settings.cache_clear()


@pytest.mark.mysql
def test_migration_0045_aborts_on_live_cross_user_dup(monkeypatch):
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(get_test_database_url(), pool_pre_ping=True)
    reset_test_database(engine, create_schema=False)
    monkeypatch.setenv("GEO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("GEO_DATABASE_URL", get_test_database_url())
    get_settings.cache_clear()

    try:
        cfg = AlembicConfig("alembic.ini")
        command.upgrade(cfg, "0044")

        with engine.begin() as conn:
            pid = _wechat_platform_id(conn)
            for uname in ("mig-a", "mig-b"):
                uid = _seed_user(conn, uname)
                conn.execute(
                    text(
                        "INSERT INTO accounts (user_id, platform_id, display_name, "
                        "platform_user_id, status, is_deleted, distribution_enabled, "
                        "created_at, updated_at) VALUES "
                        "(:uid, :pid, :nm, 'wxLIVE', 'unknown', 0, 1, NOW(), NOW())"
                    ),
                    {"uid": uid, "pid": pid, "nm": uname},
                )

        with pytest.raises(Exception) as excinfo:
            command.upgrade(cfg, "0045")
        assert "wxLIVE" in str(excinfo.value) or "重复" in str(excinfo.value)
    finally:
        reset_test_database(engine, create_schema=False)
        engine.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)
        get_settings.cache_clear()
