"""账号登录态夜间保活：纯函数单测 + MySQL 选账号 + 刷新/编排 monkeypatch 测试。"""

from __future__ import annotations

import datetime as dt
import random
import time as _time
from zoneinfo import ZoneInfo

import pytest

from server.app.core.config import Settings
from server.app.modules.accounts import keepalive as ka
from server.app.modules.accounts.models import Account
from server.app.modules.system.models import Platform, User
from server.tests.utils import build_test_app

_TZ = ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------------
# Task 1: 配置项
# ---------------------------------------------------------------------------


def test_keepalive_settings_defaults():
    s = Settings(jwt_secret="x")
    assert s.account_keepalive_enabled is False
    assert s.account_keepalive_window_start == "23:00"
    assert s.account_keepalive_window_end == "03:00"
    assert s.account_keepalive_min_gap_seconds == 30
    assert s.account_keepalive_max_gap_seconds == 600
    assert s.account_keepalive_poll_seconds == 120
    assert s.account_keepalive_check_timeout_seconds == 120


# ---------------------------------------------------------------------------
# Task 2: 时间窗口纯函数
# ---------------------------------------------------------------------------


def test_parse_hhmm():
    assert ka.parse_hhmm("23:00") == dt.time(23, 0)
    assert ka.parse_hhmm("03:30") == dt.time(3, 30)


def test_in_window_overnight():
    start, end = dt.time(23, 0), dt.time(3, 0)
    assert ka.in_keepalive_window(start, end, dt.datetime(2026, 6, 25, 1, 30, tzinfo=_TZ)) is True
    assert ka.in_keepalive_window(start, end, dt.datetime(2026, 6, 25, 23, 30, tzinfo=_TZ)) is True
    assert ka.in_keepalive_window(start, end, dt.datetime(2026, 6, 25, 12, 0, tzinfo=_TZ)) is False


def test_window_start_instant_overnight_uses_previous_day():
    # now=01:30 本地 → 本窗口起点是「昨天 23:00」本地 = UTC-naive 15:00 (CST=UTC+8)
    now = dt.datetime(2026, 6, 25, 1, 30, tzinfo=_TZ)
    ws = ka.window_start_instant(dt.time(23, 0), now)
    assert ws == dt.datetime(2026, 6, 24, 15, 0)  # 23:00 CST 6/24 == 15:00 UTC 6/24


def test_window_end_instant_overnight_next_occurrence():
    # now=23:30 本地 → 本窗口止点是「明天 03:00」本地 = UTC-naive 19:00 当天
    now = dt.datetime(2026, 6, 25, 23, 30, tzinfo=_TZ)
    we = ka.window_end_instant(dt.time(3, 0), now)
    assert we == dt.datetime(2026, 6, 25, 19, 0)  # 03:00 CST 6/26 == 19:00 UTC 6/25


# ---------------------------------------------------------------------------
# Task 3: compute_next_gap
# ---------------------------------------------------------------------------


def test_compute_gap_within_bounds():
    rng = random.Random(42)
    for _ in range(50):
        g = ka.compute_next_gap(10000.0, 5, 30.0, 600.0, rng)
        assert 30.0 <= g <= 600.0


def test_compute_gap_compresses_when_many_due():
    # 剩余窗口 600s，剩 100 个 → cap=6s < min_gap → 退化为恒定 min_gap=30
    rng = random.Random(1)
    g = ka.compute_next_gap(600.0, 100, 30.0, 600.0, rng)
    assert g == 30.0


def test_compute_gap_caps_at_remaining_per_account():
    # 剩余窗口 1000s，剩 10 个 → cap=100s → 上界 100，间隔落 [30,100]
    rng = random.Random(7)
    for _ in range(50):
        g = ka.compute_next_gap(1000.0, 10, 30.0, 600.0, rng)
        assert 30.0 <= g <= 100.0


def test_compute_gap_no_div_by_zero_when_last_account():
    rng = random.Random(3)
    g = ka.compute_next_gap(600.0, 0, 30.0, 600.0, rng)
    assert 30.0 <= g <= 600.0


def test_compute_gap_negative_window_degrades_to_min():
    rng = random.Random(9)
    g = ka.compute_next_gap(-50.0, 5, 30.0, 600.0, rng)
    assert g == 30.0


# ---------------------------------------------------------------------------
# Task 4: select_due_account_ids（MySQL）
# ---------------------------------------------------------------------------


def _platform(db) -> Platform:
    p = Platform(code="toutiao", name="头条号", base_url="https://mp.toutiao.com", enabled=True)
    db.add(p)
    db.flush()
    return p


def _user(db) -> User:
    u = User(username="ka_owner", role="operator", is_active=True, must_change_password=False)
    u.set_password("pw-123456")
    db.add(u)
    db.flush()
    return u


def _account(
    db,
    *,
    pid,
    uid,
    name,
    status="valid",
    state_path="x/s.json",
    last_checked_at=None,
    is_deleted=False,
    merged_into=None,
) -> Account:
    a = Account(
        user_id=uid,
        platform_id=pid,
        display_name=name,
        status=status,
        state_path=state_path,
        last_checked_at=last_checked_at,
        is_deleted=is_deleted,
        merged_into=merged_into,
    )
    db.add(a)
    db.flush()
    return a


@pytest.mark.mysql
def test_select_due_filters_and_orders(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        window_start = dt.datetime(2026, 6, 24, 15, 0)  # 本窗口起点（UTC-naive）
        with test_app.session_factory() as db:
            p = _platform(db)
            u = _user(db)
            # 应入选：valid 浏览器账号，本窗口未刷（NULL / 早于 window_start）
            never = _account(db, pid=p.id, uid=u.id, name="never", last_checked_at=None)
            old = _account(
                db,
                pid=p.id,
                uid=u.id,
                name="old",
                last_checked_at=dt.datetime(2026, 6, 24, 10, 0),
            )
            # 应排除：本窗口已刷（>= window_start）
            _account(
                db,
                pid=p.id,
                uid=u.id,
                name="fresh",
                last_checked_at=dt.datetime(2026, 6, 24, 16, 0),
            )
            # 应排除：非 valid / API 账号(state_path NULL) / 已删 / 已合并
            _account(db, pid=p.id, uid=u.id, name="expired", status="expired")
            _account(db, pid=p.id, uid=u.id, name="api", state_path=None)
            _account(db, pid=p.id, uid=u.id, name="deleted", is_deleted=True)
            canonical = _account(db, pid=p.id, uid=u.id, name="canon")
            _account(db, pid=p.id, uid=u.id, name="merged", merged_into=canonical.id)
            db.commit()
            never_id, old_id, canon_id = never.id, old.id, canonical.id

        with test_app.session_factory() as db:
            due = ka.select_due_account_ids(db, window_start)

        # 过滤：恰好选出 never/old/canon 三个 valid 浏览器账号（排除 expired/api/deleted/merged/fresh）
        assert set(due) == {never_id, old_id, canon_id}
        assert len(due) == 3
        # 顺序（ASC，NULL 最前）：last_checked_at 为 NULL 的 never、canon 排在有时间戳的 old 之前。
        # never 与 canon 同为 NULL，相对顺序由 SQL 未定义——故不断言二者先后，只断言 NULL 组在 old 之前。
        assert due.index(never_id) < due.index(old_id)
        assert due.index(canon_id) < due.index(old_id)
    finally:
        test_app.cleanup()


# ---------------------------------------------------------------------------
# Task 5: refresh_one_account（MySQL）
# ---------------------------------------------------------------------------


@pytest.mark.mysql
def test_refresh_flip_to_expired_alerts(monkeypatch):
    test_app = build_test_app(monkeypatch)
    sent = []
    monkeypatch.setattr(
        "server.app.shared.feishu.send_text",
        lambda title, message, level="info": sent.append((title, level)) or True,
    )

    def fake_check(db, account, payload):
        account.status = "expired"  # 模拟检测发现失效
        return account

    monkeypatch.setattr("server.app.modules.accounts.auth.check_account", fake_check)

    try:
        with test_app.session_factory() as db:
            p = _platform(db)
            u = _user(db)
            acc = _account(db, pid=p.id, uid=u.id, name="flip", status="valid")
            db.commit()
            acc_id = acc.id

        r = ka.refresh_one_account(test_app.session_factory, acc_id, check_timeout_s=30)
        assert r == "flipped_expired"
        assert any("失效" in t for t, _ in sent)
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_refresh_stays_valid_no_alert(monkeypatch):
    test_app = build_test_app(monkeypatch)
    sent = []
    monkeypatch.setattr(
        "server.app.shared.feishu.send_text",
        lambda title, message, level="info": sent.append(title) or True,
    )
    monkeypatch.setattr(
        "server.app.modules.accounts.auth.check_account",
        lambda db, account, payload: account,  # 保持 valid
    )
    try:
        with test_app.session_factory() as db:
            p = _platform(db)
            u = _user(db)
            acc = _account(db, pid=p.id, uid=u.id, name="ok", status="valid")
            db.commit()
            acc_id = acc.id
        r = ka.refresh_one_account(test_app.session_factory, acc_id, check_timeout_s=30)
        assert r == "refreshed_valid"
        assert sent == []
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_refresh_lock_busy_swallowed(monkeypatch):
    from server.app.shared.errors import ClientError

    test_app = build_test_app(monkeypatch)

    def boom(db, account, payload):
        raise ClientError("账号正在执行发布或登录操作")

    monkeypatch.setattr("server.app.modules.accounts.auth.check_account", boom)
    try:
        with test_app.session_factory() as db:
            p = _platform(db)
            u = _user(db)
            acc = _account(db, pid=p.id, uid=u.id, name="busy", status="valid")
            db.commit()
            acc_id = acc.id
        r = ka.refresh_one_account(test_app.session_factory, acc_id, check_timeout_s=30)
        assert r == "lock_busy"
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_refresh_timeout_alerts_and_returns(monkeypatch):
    test_app = build_test_app(monkeypatch)
    sent = []
    monkeypatch.setattr(
        "server.app.shared.feishu.send_text",
        lambda title, message, level="info": sent.append((title, level)) or True,
    )

    def slow(db, account, payload):
        _time.sleep(1.0)
        return account

    monkeypatch.setattr("server.app.modules.accounts.auth.check_account", slow)
    try:
        with test_app.session_factory() as db:
            p = _platform(db)
            u = _user(db)
            acc = _account(db, pid=p.id, uid=u.id, name="slow", status="valid")
            db.commit()
            acc_id = acc.id
        r = ka.refresh_one_account(test_app.session_factory, acc_id, check_timeout_s=0.2)
        assert r == "timeout"
        assert any("超时" in t for t, _ in sent)
    finally:
        test_app.cleanup()


# ---------------------------------------------------------------------------
# Task 6: run_keepalive_once
# ---------------------------------------------------------------------------


class _FakeKaSettings:
    account_keepalive_window_start = "23:00"
    account_keepalive_window_end = "03:00"
    account_keepalive_min_gap_seconds = 30
    account_keepalive_max_gap_seconds = 600
    account_keepalive_check_timeout_seconds = 120


def test_run_once_out_of_window_skips(monkeypatch):
    monkeypatch.setattr(ka, "get_settings", lambda: _FakeKaSettings())
    now = dt.datetime(2026, 6, 25, 12, 0, tzinfo=_TZ)  # 窗口外
    r = ka.run_keepalive_once(lambda: None, now, random.Random(0))
    assert r["processed"] is False and r["in_window"] is False


class _DummyDB:
    def close(self):
        pass


def test_run_once_no_due_in_window(monkeypatch):
    monkeypatch.setattr(ka, "get_settings", lambda: _FakeKaSettings())
    monkeypatch.setattr(ka, "select_due_account_ids", lambda db, ws: [])
    now = dt.datetime(2026, 6, 25, 1, 0, tzinfo=_TZ)  # 窗口内
    r = ka.run_keepalive_once(lambda: _DummyDB(), now, random.Random(0))
    assert r["processed"] is False and r["in_window"] is True and r["remaining_due"] == 0


def test_run_once_refreshes_oldest_and_returns_gap(monkeypatch):
    monkeypatch.setattr(ka, "get_settings", lambda: _FakeKaSettings())
    monkeypatch.setattr(ka, "select_due_account_ids", lambda db, ws: [7, 8, 9])
    calls = []

    def _fake_refresh(sf, aid, *, check_timeout_s):
        calls.append(aid)
        return "refreshed_valid"

    monkeypatch.setattr(ka, "refresh_one_account", _fake_refresh)
    now = dt.datetime(2026, 6, 25, 1, 0, tzinfo=_TZ)
    r = ka.run_keepalive_once(lambda: _DummyDB(), now, random.Random(0))
    assert calls == [7]  # 恰好刷一个，且是 due[0]（最旧优先）
    assert r["processed"] is True and r["result"] == "refreshed_valid"
    assert r["remaining_due"] == 2
    assert 30.0 <= r["next_gap_seconds"] <= 600.0


# ---------------------------------------------------------------------------
# Task 7: start_keepalive / stop_keepalive
# ---------------------------------------------------------------------------


def test_start_keepalive_disabled_returns_false(monkeypatch):
    class _Off:
        account_keepalive_enabled = False
        scheduler_tz = "Asia/Shanghai"

    monkeypatch.setattr(ka, "get_settings", lambda: _Off())
    assert ka.start_keepalive(lambda: None) is False


def test_start_keepalive_runs_one_round_then_stops(monkeypatch):
    # 重置模块级全局，避免前序测试遗留的线程/停止位污染本用例（start_keepalive 见活线程会静默返回 False）
    ka._stop.clear()
    ka._thread = None
    rounds = []

    class _On:
        account_keepalive_enabled = True
        scheduler_tz = "Asia/Shanghai"
        account_keepalive_poll_seconds = 1

    monkeypatch.setattr(ka, "get_settings", lambda: _On())

    def fake_once(sf, now, rng):
        rounds.append(1)
        ka.stop_keepalive()  # 跑一轮即请求停止
        return {"processed": False, "in_window": False}

    monkeypatch.setattr(ka, "run_keepalive_once", fake_once)

    try:
        assert ka.start_keepalive(lambda: None) is True
        ka._thread.join(timeout=5)
        assert ka._thread.is_alive() is False
        assert len(rounds) >= 1
    finally:
        ka.stop_keepalive()
        if ka._thread is not None:
            ka._thread.join(timeout=5)
        ka._stop.clear()
        ka._thread = None
    assert len(rounds) >= 1


# ---------------------------------------------------------------------------
# Task 8: worker integration
# ---------------------------------------------------------------------------


@pytest.mark.mysql
def test_worker_main_starts_and_stops_keepalive(monkeypatch):
    # build_test_app 先 setenv(GEO_DATABASE_URL) 再 import executor，避免 db.session 即时建引擎在隔离运行时失败
    test_app = build_test_app(monkeypatch)
    try:
        from server.worker import executor as ex

        started = {}
        stopped = {}

        def _fake_start(sf):
            started["sf"] = sf
            return True

        monkeypatch.setattr("server.app.modules.accounts.keepalive.start_keepalive", _fake_start)
        monkeypatch.setattr(
            "server.app.modules.accounts.keepalive.stop_keepalive",
            lambda: stopped.setdefault("called", True),
        )
        # 让 main() 立即收敛：startup/login loop/periodic recovery/claim 全 noop，心跳触发 shutdown
        monkeypatch.setattr(ex, "_startup", lambda db: None)
        monkeypatch.setattr(ex, "_account_login_loop", lambda: None)
        monkeypatch.setattr(ex, "_periodic_recovery", lambda db: None)
        monkeypatch.setattr(ex, "_claim_next_task", lambda db: None)

        def _stop_now(db):
            ex._shutdown = True

        monkeypatch.setattr(ex, "_write_worker_heartbeat", _stop_now)
        monkeypatch.setattr(
            "server.app.modules.accounts.login_broker.login_broker.shutdown", lambda: None
        )

        monkeypatch.setattr(ex, "_shutdown", False)
        try:
            ex.main()
        finally:
            ex._shutdown = False

        assert started.get("sf") is ex.SessionLocal  # 传入的是真正的 session 工厂
        assert stopped.get("called") is True
    finally:
        test_app.cleanup()
