"""跨进程 Chromium profile 锁（browser.BrowserProfileLock）契约。

这把锁是发布 / 登录跨进程互斥的核心：同一持久化 Chromium profile 同一时刻只能被一个 owner
持有，否则两个进程同开一个 profile 会损坏它 / 抢崩。owner 崩溃后留下的死锁必须能被新请求按
租约过期接管，否则账号永久无法再登录。此前整块（browser.py:95-224）零测试。

锁函数通过 _get_db() → SessionLocal 落库，build_test_app 已把 SessionLocal patch 到测试库，
故这里直接调真实函数即可命中测试 DB。
"""

from datetime import timedelta

import pytest

from server.app.core.time import utcnow
from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_profile_lock_mutex_reentrant_heartbeat_takeover_release(monkeypatch):
    from server.app.modules.accounts import browser
    from server.app.modules.accounts.models import BrowserProfileLock

    app = build_test_app(monkeypatch)
    try:
        key = "toutiao/lock-key"

        # ownerA 抢到
        assert browser.try_acquire_profile_lock(key, owner_kind="publish", owner_id=1) is True
        # ownerB 在 A 持有期间抢 → False（互斥）
        assert browser.try_acquire_profile_lock(key, owner_kind="publish", owner_id=2) is False
        # 同一 owner 重入视为续租，仍 True（幂等可重入）
        assert browser.try_acquire_profile_lock(key, owner_kind="publish", owner_id=1) is True

        # heartbeat 把 lease_until 往后推（长操作期间防被当过期回收）
        with app.session_factory() as db:
            before = db.get(BrowserProfileLock, key).lease_until
        browser.heartbeat_profile_lock(key, owner_kind="publish", owner_id=1)
        with app.session_factory() as db:
            after = db.get(BrowserProfileLock, key).lease_until
        assert after >= before

        # 把租约置为过期 → ownerB 接管（删死锁 + 抢到）：owner 崩溃后的死锁恢复路径
        with app.session_factory() as db:
            lock = db.get(BrowserProfileLock, key)
            lock.lease_until = utcnow() - timedelta(seconds=10)
            db.commit()
        assert browser.try_acquire_profile_lock(key, owner_kind="publish", owner_id=2) is True
        with app.session_factory() as db:
            assert db.get(BrowserProfileLock, key).owner_id == "2"  # owner_id 以字符串存

        # release 释放 → 行删除，任何人都能重新抢到
        browser.release_profile_lock(key, owner_kind="publish", owner_id=2)
        with app.session_factory() as db:
            assert db.get(BrowserProfileLock, key) is None
        assert browser.try_acquire_profile_lock(key, owner_kind="publish", owner_id=3) is True
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_profile_lock_distinct_owner_kind_not_confused(monkeypatch):
    """同一 owner_id 但不同 owner_kind（publish vs login）视为不同 owner，不应误判持有。"""
    from server.app.modules.accounts import browser

    app = build_test_app(monkeypatch)
    try:
        key = "toutiao/kind-key"
        assert browser.try_acquire_profile_lock(key, owner_kind="publish", owner_id=7) is True
        # 同 id、不同 kind：锁已被 publish:7 持有 → login:7 抢不到
        assert browser.try_acquire_profile_lock(key, owner_kind="login", owner_id=7) is False
        # 用错 kind 释放不应误删 publish:7 的锁
        browser.release_profile_lock(key, owner_kind="login", owner_id=7)
        assert browser.try_acquire_profile_lock(key, owner_kind="login", owner_id=8) is False
    finally:
        app.cleanup()
