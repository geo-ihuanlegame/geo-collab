"""回归：alembic 在进程内跑迁移时不得禁用应用既有 logger。

根因见 server/alembic/env.py：fileConfig 默认 disable_existing_loggers=True，会把
server.app.* 等已存在 logger 的 .disabled 置 True 并泄漏到同进程后续测试——曾导致
nightly 全量单进程跑里 test_feishu 的 caplog 捕获不到 warning（PR 分片 CI 因把两个测试
分到不同进程而侥幸不暴露）。env.py 已改为 disable_existing_loggers=False；本测试守住它。
"""

from __future__ import annotations

import logging

import pytest
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine

from alembic import command
from server.app.core.config import get_settings
from server.tests.utils import get_test_database_url, reset_test_database


@pytest.mark.mysql
def test_alembic_upgrade_does_not_disable_app_loggers(monkeypatch):
    engine = create_engine(get_test_database_url(), pool_pre_ping=True)
    reset_test_database(engine, create_schema=False)
    monkeypatch.setenv("GEO_DATABASE_URL", get_test_database_url())
    get_settings.cache_clear()

    sentinel = logging.getLogger("server.app.shared.feishu")
    sentinel.disabled = False
    try:
        cfg = AlembicConfig("alembic.ini")
        command.upgrade(cfg, "head")  # 加载 server/alembic/env.py → 触发 fileConfig

        assert sentinel.disabled is False, (
            "alembic env.py 的 fileConfig 必须带 disable_existing_loggers=False，"
            "否则会禁用应用 logger 并污染同进程后续测试"
        )
        assert sentinel.isEnabledFor(logging.WARNING)
    finally:
        reset_test_database(engine, create_schema=False)
        engine.dispose()
        get_settings.cache_clear()
