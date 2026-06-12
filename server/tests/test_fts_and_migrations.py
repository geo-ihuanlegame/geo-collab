from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect, text

from alembic import command
from server.app.core.config import get_settings
from server.tests.utils import (
    build_test_app,
    get_test_database_url,
    invalidate_test_schema,
    reset_test_database,
)


def _new_temp_data_dir() -> Path:
    return Path(tempfile.gettempdir()) / "geo-test-data" / uuid.uuid4().hex


def _tiptap_doc() -> dict:
    return {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}],
    }


@pytest.mark.mysql
def test_search_falls_back_to_like_when_fulltext_index_missing(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client

    try:
        resp = client.post(
            "/api/articles",
            json={
                "title": "Fallback Article ABC",
                "author": "TestAuthor",
                "plain_text": "fallback body",
                "content_json": _tiptap_doc(),
            },
        )
        assert resp.status_code == 200
        article_id = resp.json()["id"]

        with test_app.session_factory() as session:
            session.execute(text("DROP INDEX ft_articles ON articles"))
            session.commit()

        resp = client.get("/api/articles", params={"q": "Fallback"})
        assert resp.status_code == 200
        assert any(item["id"] == article_id for item in resp.json())

        resp = client.get("/api/articles", params={"q": "xyznotfound999"})
        assert resp.status_code == 200
        assert resp.json() == []
    finally:
        test_app.cleanup()
        # 本测试用裸 SQL DROP 了 ft_articles 索引，标记共享数据库结构失效，
        # 让下个 build_test_app 全量重建（否则后续全文检索测试会缺索引）。
        invalidate_test_schema()


@pytest.mark.mysql
def test_alembic_upgrade_from_empty_mysql_to_head(monkeypatch):
    data_dir = _new_temp_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(get_test_database_url(), pool_pre_ping=True)
    reset_test_database(engine, create_schema=False)
    monkeypatch.setenv("GEO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("GEO_DATABASE_URL", get_test_database_url())
    get_settings.cache_clear()

    try:
        cfg = AlembicConfig("alembic.ini")
        command.upgrade(cfg, "head")

        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        expected_tables = {
            "platforms",
            "accounts",
            "assets",
            "articles",
            "article_body_assets",
            "article_groups",
            "article_group_items",
            "publish_tasks",
            "publish_task_accounts",
            "publish_records",
            "task_logs",
            "users",
        }
        assert expected_tables.issubset(tables), f"Missing tables: {expected_tables - tables}"

        with engine.connect() as conn:
            rows = conn.execute(
                text("SHOW INDEX FROM articles WHERE Key_name = 'ft_articles'")
            ).fetchall()
        assert rows, "MySQL FULLTEXT index ft_articles was not created"
    finally:
        reset_test_database(engine, create_schema=False)
        engine.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)
        get_settings.cache_clear()


@pytest.mark.mysql
def test_mysql_fulltext_index_contains_plain_text(monkeypatch):
    data_dir = _new_temp_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(get_test_database_url(), pool_pre_ping=True)
    reset_test_database(engine, create_schema=False)
    monkeypatch.setenv("GEO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("GEO_DATABASE_URL", get_test_database_url())
    get_settings.cache_clear()

    try:
        cfg = AlembicConfig("alembic.ini")
        command.upgrade(cfg, "head")

        with engine.connect() as conn:
            rows = (
                conn.execute(text("SHOW INDEX FROM articles WHERE Key_name = 'ft_articles'"))
                .mappings()
                .all()
            )
        indexed_columns = {row["Column_name"] for row in rows}
        assert {"title", "author", "plain_text"}.issubset(indexed_columns)
    finally:
        reset_test_database(engine, create_schema=False)
        engine.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)
        get_settings.cache_clear()
