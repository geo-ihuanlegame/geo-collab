"""recover_stuck_login_sessions 契约：worker 启动时把残留的非终态登录会话置 cancelled
并清掉它们的 login profile 锁，但不误伤 publish 锁，也不动已终态会话。

交互式登录浏览器只活在 worker 进程内，worker 一重启全死。任何存活的非终态登录会话都是
僵死的——若不复位，它持有的 profile 锁会永久把账号挡在登录之外（生产死锁事故的根因）。
"""

import pytest

from server.tests.test_accounts_api import install_fake_driver, write_storage_state
from server.tests.utils import build_test_app


@pytest.mark.mysql
def test_recover_cancels_active_login_session_and_releases_lock(monkeypatch):
    from server.app.modules.accounts import browser
    from server.app.modules.accounts.auth import (
        LOGIN_STATUS_ACTIVE,
        LOGIN_STATUS_FINISHED,
        recover_stuck_login_sessions,
    )
    from server.app.modules.accounts.models import (
        Account,
        AccountLoginSession,
        BrowserProfileLock,
    )
    from server.app.modules.accounts.service import profile_key_from_state_path

    test_app = build_test_app(monkeypatch)
    client = test_app.client
    install_fake_driver(monkeypatch)
    try:
        write_storage_state(test_app.data_dir, "demo")
        account = client.post(
            "/api/accounts/toutiao/login",
            json={"display_name": "stuck-login", "account_key": "demo", "use_browser": False},
        ).json()

        db = test_app.session_factory()
        try:
            state_path = db.get(Account, account["id"]).state_path
            profile_key = profile_key_from_state_path(state_path)

            # 残留的 active 登录会话（worker 已死），持有该账号的 login 锁
            stuck = AccountLoginSession(
                id="stuck-act",
                account_id=account["id"],
                platform_code="toutiao",
                account_key="demo",
                channel="chromium",
                status=LOGIN_STATUS_ACTIVE,
                worker_id="dead-worker-8",
                queue_reason=None,
                previous_status="valid",
            )
            # 已终态会话：复位不应改写它
            done = AccountLoginSession(
                id="done-fin",
                account_id=account["id"],
                platform_code="toutiao",
                account_key="demo",
                channel="chromium",
                status=LOGIN_STATUS_FINISHED,
            )
            db.add_all([stuck, done])
            db.commit()

            assert (
                browser.try_acquire_profile_lock(
                    profile_key, owner_kind="login", owner_id="stuck-act"
                )
                is True
            )
            # 一把无关的 publish 锁——复位绝不能误删
            assert (
                browser.try_acquire_profile_lock(
                    "toutiao/other-publish", owner_kind="publish", owner_id=99
                )
                is True
            )

            recover_stuck_login_sessions(db)

            db.refresh(stuck)
            db.refresh(done)
            assert stuck.status == "cancelled"
            assert stuck.worker_id is None
            assert stuck.queue_reason is None
            assert done.status == LOGIN_STATUS_FINISHED  # 终态不被触碰

            # login 锁被清掉 → 账号可以重新登录
            assert db.get(BrowserProfileLock, profile_key) is None
            # publish 锁原样保留
            assert db.get(BrowserProfileLock, "toutiao/other-publish") is not None
        finally:
            db.close()
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_expire_stale_active_login_session_releases_lock(monkeypatch):
    """worker 运行期间的周期兜底：被遗弃很久的 active 登录会话（关标签页/刷新/崩溃，
    没走 finish/cancel）会被当作僵死收尾——停浏览器、释放锁、置 cancelled，账号能重新登录。
    刚进入 active 的真实登录（未超时）和 publish 锁绝不能被误杀。这堵的是 #85 死锁的残留路径：
    active 锁被 worker 心跳无限续租、租约永不过期，且启动复位只在重启时跑。
    """
    from datetime import timedelta

    from server.app.core.time import utcnow
    from server.app.modules.accounts import browser
    from server.app.modules.accounts.auth import (
        LOGIN_STATUS_ACTIVE,
        expire_stale_login_sessions,
    )
    from server.app.modules.accounts.models import (
        Account,
        AccountLoginSession,
        BrowserProfileLock,
    )
    from server.app.modules.accounts.service import profile_key_from_state_path

    test_app = build_test_app(monkeypatch)
    client = test_app.client
    install_fake_driver(monkeypatch)
    try:
        write_storage_state(test_app.data_dir, "demo")
        stale_acc = client.post(
            "/api/accounts/toutiao/login",
            json={"display_name": "stale", "account_key": "demo", "use_browser": False},
        ).json()
        write_storage_state(test_app.data_dir, "fresh")
        fresh_acc = client.post(
            "/api/accounts/toutiao/login",
            json={"display_name": "fresh", "account_key": "fresh", "use_browser": False},
        ).json()

        db = test_app.session_factory()
        try:
            stale_key = profile_key_from_state_path(db.get(Account, stale_acc["id"]).state_path)
            fresh_key = profile_key_from_state_path(db.get(Account, fresh_acc["id"]).state_path)
            now = utcnow()

            # 被遗弃很久的 active 会话（关标签页/崩溃，没走 finish/cancel）
            stale = AccountLoginSession(
                id="stale-act",
                account_id=stale_acc["id"],
                platform_code="toutiao",
                account_key="demo",
                channel="chromium",
                status=LOGIN_STATUS_ACTIVE,
                worker_id="w1",
                previous_status="valid",
                updated_at=now - timedelta(seconds=3600),
            )
            # 刚进入 active 的真实登录——不能误杀
            fresh = AccountLoginSession(
                id="fresh-act",
                account_id=fresh_acc["id"],
                platform_code="toutiao",
                account_key="fresh",
                channel="chromium",
                status=LOGIN_STATUS_ACTIVE,
                worker_id="w1",
                previous_status="valid",
                updated_at=now,
            )
            db.add_all([stale, fresh])
            db.commit()

            assert (
                browser.try_acquire_profile_lock(
                    stale_key, owner_kind="login", owner_id="stale-act"
                )
                is True
            )
            assert (
                browser.try_acquire_profile_lock(
                    fresh_key, owner_kind="login", owner_id="fresh-act"
                )
                is True
            )
            assert (
                browser.try_acquire_profile_lock(
                    "toutiao/other-publish", owner_kind="publish", owner_id=99
                )
                is True
            )

            expired = expire_stale_login_sessions(db, worker_id="w1", max_active_seconds=1800)

            assert expired == 1
            db.refresh(stale)
            db.refresh(fresh)
            # 超时会话被收尾、锁释放 → 账号可重新登录
            assert stale.status == "cancelled"
            assert db.get(BrowserProfileLock, stale_key) is None
            # 新鲜的真实登录与无关的 publish 锁原样保留
            assert fresh.status == LOGIN_STATUS_ACTIVE
            assert db.get(BrowserProfileLock, fresh_key) is not None
            assert db.get(BrowserProfileLock, "toutiao/other-publish") is not None
        finally:
            db.close()
    finally:
        test_app.cleanup()
