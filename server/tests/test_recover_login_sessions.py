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
