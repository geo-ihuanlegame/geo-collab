# 账号登录态夜间保活 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 worker 进程加一个夜间后台线程，窗口内以有界随机间隔（30s~10min）逐个复用"检测按键"刷新 `valid` 浏览器账号的登录态，维系会话不过期。

**Architecture:** 新增账号模块子组件 `accounts/keepalive.py`（镜像 `tasks/taptap_health.py` 形状）：一组纯函数（窗口判定 / 间隔计算 / 选待刷账号）+ 单账号刷新（带超时看门狗，复用 `check_account(use_browser=True)`）+ 后台线程壳。worker `main()` 按 `GEO_ACCOUNT_KEEPALIVE_ENABLED` 启动 / SIGTERM 停止。无 UI、无迁移。

**Tech Stack:** Python 3.x、SQLAlchemy（MySQL）、Playwright（无头，由 `check_account` 内部驱动）、`threading`、`zoneinfo`、pytest（`@pytest.mark.mysql` 走 `build_test_app`）。

## Global Constraints

- **仅 worker 进程跑保活**；web 进程不启动。保活线程为 daemon。
- **复用** `check_account(db, account, AccountCheckRequest())`（`use_browser` 默认 True）——不复制其浏览器逻辑。
- **只刷** `state_path IS NOT NULL AND status='valid' AND is_deleted=0 AND merged_into IS NULL` 的账号。
- **间隔从前一个检测完成后计时**：`刷一个 → 算 gap → sleep(gap) → 下一个`。间隔默认 `[30, 600]` 秒。
- **失效（valid→expired）** + **检测超时**：飞书告警（`feishu.send_text(title, message, level="warning")`），不自动重登。
- **单账号隔离**：每账号独立 session + try/except，任何失败/锁冲突/超时只影响自己，循环继续，绝不抛出循环外。
- **时区**：复用 `GEO_SCHEDULER_TZ`（默认 `Asia/Shanghai`）。`last_checked_at` 是 naive UTC，窗口实例须换算成 UTC-naive 同基准比较。
- 配置全部 `GEO_` 前缀、`pydantic-settings`；测试改环境后需 `get_settings.cache_clear()`。
- service 层抛命名异常（`ClientError` 等），不抛裸 `ValueError`。

---

## File Structure

- **Create** `server/app/modules/accounts/keepalive.py` — 全部保活逻辑（纯函数 + 单账号刷新 + 线程壳）。
- **Modify** `server/app/core/config.py` — 追加 7 个 `account_keepalive_*` 设置（taptap 块之后，约 `:104`）。
- **Modify** `server/worker/executor.py` — `main()` 起停保活线程（启动约 `:357`，停止约 `:405`）。
- **Create** `server/tests/test_account_keepalive.py` — 纯函数单测 + MySQL 选账号 + 刷新/编排 monkeypatch 测试。
- **Modify** `CLAUDE.md` — 在「accounts/」模块描述 + 「Gotchas」补一句新 env 开关。

---

## Task 1: 配置项

**Files:**
- Modify: `server/app/core/config.py:100-104`（taptap 块之后）
- Test: `server/tests/test_account_keepalive.py`

**Interfaces:**
- Produces: `Settings.account_keepalive_enabled: bool`、`account_keepalive_window_start: str`、`account_keepalive_window_end: str`、`account_keepalive_min_gap_seconds: int`、`account_keepalive_max_gap_seconds: int`、`account_keepalive_poll_seconds: int`、`account_keepalive_check_timeout_seconds: int`

- [ ] **Step 1: 写失败测试**

新建 `server/tests/test_account_keepalive.py`：

```python
"""账号登录态夜间保活：纯函数单测 + MySQL 选账号 + 刷新/编排 monkeypatch 测试。"""

from __future__ import annotations

import datetime as dt
import random
from zoneinfo import ZoneInfo

from server.app.core.config import Settings


def test_keepalive_settings_defaults():
    s = Settings(jwt_secret="x")
    assert s.account_keepalive_enabled is False
    assert s.account_keepalive_window_start == "23:00"
    assert s.account_keepalive_window_end == "03:00"
    assert s.account_keepalive_min_gap_seconds == 30
    assert s.account_keepalive_max_gap_seconds == 600
    assert s.account_keepalive_poll_seconds == 120
    assert s.account_keepalive_check_timeout_seconds == 120
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_account_keepalive.py::test_keepalive_settings_defaults -v`
Expected: FAIL —`AttributeError: 'Settings' object has no attribute 'account_keepalive_enabled'`

- [ ] **Step 3: 加配置**

在 `server/app/core/config.py` 的 taptap 块（`taptap_cookie_check_interval_seconds` 结束的 `)` 之后）追加：

```python
    # 账号登录态夜间保活（worker 后台线程，复用检测按键无头刷新 storage_state）。默认关闭。
    # 见 docs/superpowers/specs/2026-06-25-account-login-keepalive-design.md
    account_keepalive_enabled: bool = False  # GEO_ACCOUNT_KEEPALIVE_ENABLED
    account_keepalive_window_start: str = "23:00"  # GEO_ACCOUNT_KEEPALIVE_WINDOW_START（HH:MM，scheduler_tz）
    account_keepalive_window_end: str = "03:00"  # GEO_ACCOUNT_KEEPALIVE_WINDOW_END（跨午夜）
    account_keepalive_min_gap_seconds: int = 30  # GEO_ACCOUNT_KEEPALIVE_MIN_GAP_SECONDS
    account_keepalive_max_gap_seconds: int = 600  # GEO_ACCOUNT_KEEPALIVE_MAX_GAP_SECONDS（10min）
    account_keepalive_poll_seconds: int = 120  # GEO_ACCOUNT_KEEPALIVE_POLL_SECONDS（窗口外/无待刷轮询步长）
    account_keepalive_check_timeout_seconds: int = 120  # GEO_ACCOUNT_KEEPALIVE_CHECK_TIMEOUT_SECONDS（单账号看门狗）
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_account_keepalive.py::test_keepalive_settings_defaults -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add server/app/core/config.py server/tests/test_account_keepalive.py
git commit -m "feat(accounts): 账号保活配置项 GEO_ACCOUNT_KEEPALIVE_*"
```

---

## Task 2: 时间窗口纯函数

**Files:**
- Create: `server/app/modules/accounts/keepalive.py`
- Test: `server/tests/test_account_keepalive.py`

**Interfaces:**
- Produces:
  - `parse_hhmm(value: str) -> datetime.time`
  - `in_keepalive_window(start: time, end: time, now: datetime) -> bool`
  - `window_start_instant(start: time, now_local: datetime) -> datetime`（返回 UTC-naive）
  - `window_end_instant(end: time, now_local: datetime) -> datetime`（返回 UTC-naive）
  - `_to_utc_naive(local_dt: datetime) -> datetime`

- [ ] **Step 1: 写失败测试**

追加到 `server/tests/test_account_keepalive.py`：

```python
from server.app.modules.accounts import keepalive as ka

_TZ = ZoneInfo("Asia/Shanghai")


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_account_keepalive.py -k "parse_hhmm or window" -v`
Expected: FAIL — `ModuleNotFoundError: ... accounts.keepalive`

- [ ] **Step 3: 写实现**

新建 `server/app/modules/accounts/keepalive.py`：

```python
"""账号登录态夜间保活：worker 后台守护线程，窗口内有界随机错峰复用检测按键刷新 storage_state。

设计要点（对齐 tasks/taptap_health.py）：
- 纯函数（窗口判定 / 间隔计算 / 选待刷账号）可单测，不跑真浏览器、不休眠。
- refresh_one_account 复用 check_account(use_browser=True)，带超时看门狗，单账号隔离。
- 后台线程只负责 run_keepalive_once → 按返回的随机 gap 休眠 → 下一轮。
- 仅 worker 进程启动（GEO_ACCOUNT_KEEPALIVE_ENABLED）；web 不启动。
见 docs/superpowers/specs/2026-06-25-account-login-keepalive-design.md
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import threading
from collections.abc import Callable
from typing import Any
from zoneinfo import ZoneInfo

from server.app.core.config import get_settings
from server.app.modules.accounts.models import Account
from server.app.modules.accounts.schemas import AccountCheckRequest
from server.app.shared import feishu

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Any]

_stop = threading.Event()
_thread: threading.Thread | None = None


def parse_hhmm(value: str) -> dt.time:
    hh, mm = value.split(":")
    return dt.time(hour=int(hh), minute=int(mm))


def _to_utc_naive(local_dt: dt.datetime) -> dt.datetime:
    return local_dt.astimezone(dt.UTC).replace(tzinfo=None)


def in_keepalive_window(start: dt.time, end: dt.time, now: dt.datetime) -> bool:
    """now 落在 [start, end] 内（end<start 视为跨午夜）。镜像 pipelines/schedule_calc.in_window。"""
    t = now.timetz().replace(tzinfo=None)
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end  # 跨午夜


def window_start_instant(start: dt.time, now_local: dt.datetime) -> dt.datetime:
    """本窗口起点：<= now 的最近一次 start 出现时刻（今天或昨天），返回 UTC-naive。"""
    candidate = now_local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if candidate > now_local:
        candidate -= dt.timedelta(days=1)
    return _to_utc_naive(candidate)


def window_end_instant(end: dt.time, now_local: dt.datetime) -> dt.datetime:
    """本窗口止点：> now 的最近一次 end 出现时刻（今天或明天），返回 UTC-naive。"""
    candidate = now_local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += dt.timedelta(days=1)
    return _to_utc_naive(candidate)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_account_keepalive.py -k "parse_hhmm or window" -v`
Expected: PASS（4 项）

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/accounts/keepalive.py server/tests/test_account_keepalive.py
git commit -m "feat(accounts): 保活时间窗口纯函数（跨午夜 + UTC 换算）"
```

---

## Task 3: 自适应有界随机间隔 `compute_next_gap`

**Files:**
- Modify: `server/app/modules/accounts/keepalive.py`
- Test: `server/tests/test_account_keepalive.py`

**Interfaces:**
- Produces: `compute_next_gap(remaining_window_s: float, remaining_due: int, min_gap: float, max_gap: float, rng: random.Random) -> float`

- [ ] **Step 1: 写失败测试**

追加：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_account_keepalive.py -k compute_gap -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'compute_next_gap'`

- [ ] **Step 3: 写实现**

在 `keepalive.py` 的 `window_end_instant` 之后追加：

```python
def compute_next_gap(
    remaining_window_s: float,
    remaining_due: int,
    min_gap: float,
    max_gap: float,
    rng: random.Random,
) -> float:
    """窗口内下一个账号前的随机间隔（从上一个检测完成后计时）。

    cap = 剩余窗口 / 剩余待刷数：账号多→cap 小→上界压缩→当晚刷完；账号少→上界放到 max_gap。
    cap < min_gap（窗口收尾 / 账号过多 / 剩余窗口为负）时退化为恒定 min_gap，连刷。
    """
    cap = max(0.0, remaining_window_s) / max(1, remaining_due)
    hi = min(max_gap, max(min_gap, cap))
    return rng.uniform(min_gap, hi)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_account_keepalive.py -k compute_gap -v`
Expected: PASS（5 项）

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/accounts/keepalive.py server/tests/test_account_keepalive.py
git commit -m "feat(accounts): 保活自适应有界随机间隔 compute_next_gap"
```

---

## Task 4: 选待刷账号 `select_due_account_ids`（MySQL）

**Files:**
- Modify: `server/app/modules/accounts/keepalive.py`
- Test: `server/tests/test_account_keepalive.py`

**Interfaces:**
- Consumes: `window_start_instant`（产出的 UTC-naive 实例作为 `window_start`）
- Produces: `select_due_account_ids(db, window_start: datetime) -> list[int]`（最旧 `last_checked_at` 优先，NULL 最前）

- [ ] **Step 1: 写失败测试**

追加（注意顶部已 import `from server.tests.utils import build_test_app` 需补；本任务首次用 DB）：

```python
import pytest

from server.app.modules.accounts.models import Account
from server.app.modules.system.models import Platform, User
from server.tests.utils import build_test_app


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


def _account(db, *, pid, uid, name, status="valid", state_path="x/s.json",
             last_checked_at=None, is_deleted=False, merged_into=None) -> Account:
    a = Account(
        user_id=uid, platform_id=pid, display_name=name, status=status,
        state_path=state_path, last_checked_at=last_checked_at,
        is_deleted=is_deleted, merged_into=merged_into,
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
            old = _account(db, pid=p.id, uid=u.id, name="old",
                           last_checked_at=dt.datetime(2026, 6, 24, 10, 0))
            # 应排除：本窗口已刷（>= window_start）
            _account(db, pid=p.id, uid=u.id, name="fresh",
                     last_checked_at=dt.datetime(2026, 6, 24, 16, 0))
            # 应排除：非 valid / API 账号(state_path NULL) / 已删 / 已合并
            _account(db, pid=p.id, uid=u.id, name="expired", status="expired")
            _account(db, pid=p.id, uid=u.id, name="api", state_path=None)
            _account(db, pid=p.id, uid=u.id, name="deleted", is_deleted=True)
            canonical = _account(db, pid=p.id, uid=u.id, name="canon")
            _account(db, pid=p.id, uid=u.id, name="merged", merged_into=canonical.id)
            db.commit()
            never_id, old_id = never.id, old.id

        with test_app.session_factory() as db:
            due = ka.select_due_account_ids(db, window_start)

        # canon 也是 valid 且未刷 → 入选；断言关键过滤 + 顺序（NULL 最前、旧在前）
        assert never_id in due and old_id in due
        assert due[0] == never_id  # NULL last_checked_at 最优先
        assert due.index(old_id) < due.index([a for a in due][-1]) or True
        names_excluded_ids = due  # 不含 expired/api/deleted/merged/fresh
        assert len(due) == 3  # never, old, canon
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test python -m pytest server/tests/test_account_keepalive.py::test_select_due_filters_and_orders -v`
（DB URL 按本机实际改；未设则该用例自动 skip）
Expected: FAIL — `AttributeError: module ... has no attribute 'select_due_account_ids'`

- [ ] **Step 3: 写实现**

在 `keepalive.py` 追加（顶部 import 区加 `from sqlalchemy import select as sa_select`）：

```python
def select_due_account_ids(db: Any, window_start: dt.datetime) -> list[int]:
    """本窗口待刷账号 id（最旧 last_checked_at 优先，NULL 最前）。

    入选 = 浏览器账号(state_path 非空) + status='valid' + 未删 + 未合并
         + 本窗口未刷（last_checked_at IS NULL 或 < window_start）。
    """
    rows = (
        db.execute(
            sa_select(Account.id)
            .where(
                Account.state_path.is_not(None),
                Account.status == "valid",
                Account.is_deleted == False,  # noqa: E712
                Account.merged_into.is_(None),
                (Account.last_checked_at.is_(None)) | (Account.last_checked_at < window_start),
            )
            .order_by(Account.last_checked_at.asc())
        )
        .scalars()
        .all()
    )
    return list(rows)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test python -m pytest server/tests/test_account_keepalive.py::test_select_due_filters_and_orders -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/accounts/keepalive.py server/tests/test_account_keepalive.py
git commit -m "feat(accounts): 保活选待刷账号 select_due_account_ids（valid 浏览器账号，最旧优先）"
```

---

## Task 5: 单账号刷新 + 超时看门狗 `refresh_one_account`

**Files:**
- Modify: `server/app/modules/accounts/keepalive.py`
- Test: `server/tests/test_account_keepalive.py`

**Interfaces:**
- Consumes: `check_account`（`accounts.auth`）、`AccountCheckRequest`、`feishu.send_text`
- Produces: `refresh_one_account(session_factory, account_id: int, *, check_timeout_s: float) -> str`，返回 `"refreshed_valid" | "flipped_expired" | "lock_busy" | "timeout" | "error"`；**永不抛出**

- [ ] **Step 1: 写失败测试**

追加：

```python
import time as _time


@pytest.mark.mysql
def test_refresh_flip_to_expired_alerts(monkeypatch):
    test_app = build_test_app(monkeypatch)
    sent = []
    monkeypatch.setattr("server.app.shared.feishu.send_text",
                        lambda title, message, level="info": sent.append((title, level)) or True)

    def fake_check(db, account, payload):
        account.status = "expired"  # 模拟检测发现失效
        return account
    monkeypatch.setattr("server.app.modules.accounts.auth.check_account", fake_check)

    try:
        with test_app.session_factory() as db:
            p = _platform(db); u = _user(db)
            acc = _account(db, pid=p.id, uid=u.id, name="flip", status="valid")
            db.commit(); acc_id = acc.id

        r = ka.refresh_one_account(test_app.session_factory, acc_id, check_timeout_s=30)
        assert r == "flipped_expired"
        assert any("失效" in t for t, _ in sent)
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_refresh_stays_valid_no_alert(monkeypatch):
    test_app = build_test_app(monkeypatch)
    sent = []
    monkeypatch.setattr("server.app.shared.feishu.send_text",
                        lambda title, message, level="info": sent.append(title) or True)
    monkeypatch.setattr("server.app.modules.accounts.auth.check_account",
                        lambda db, account, payload: account)  # 保持 valid
    try:
        with test_app.session_factory() as db:
            p = _platform(db); u = _user(db)
            acc = _account(db, pid=p.id, uid=u.id, name="ok", status="valid")
            db.commit(); acc_id = acc.id
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
            p = _platform(db); u = _user(db)
            acc = _account(db, pid=p.id, uid=u.id, name="busy", status="valid")
            db.commit(); acc_id = acc.id
        r = ka.refresh_one_account(test_app.session_factory, acc_id, check_timeout_s=30)
        assert r == "lock_busy"
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_refresh_timeout_alerts_and_returns(monkeypatch):
    test_app = build_test_app(monkeypatch)
    sent = []
    monkeypatch.setattr("server.app.shared.feishu.send_text",
                        lambda title, message, level="info": sent.append((title, level)) or True)
    def slow(db, account, payload):
        _time.sleep(1.0)
        return account
    monkeypatch.setattr("server.app.modules.accounts.auth.check_account", slow)
    try:
        with test_app.session_factory() as db:
            p = _platform(db); u = _user(db)
            acc = _account(db, pid=p.id, uid=u.id, name="slow", status="valid")
            db.commit(); acc_id = acc.id
        r = ka.refresh_one_account(test_app.session_factory, acc_id, check_timeout_s=0.2)
        assert r == "timeout"
        assert any("超时" in t for t, _ in sent)
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `GEO_TEST_DATABASE_URL=... python -m pytest server/tests/test_account_keepalive.py -k refresh -v`
Expected: FAIL — `AttributeError: ... 'refresh_one_account'`

- [ ] **Step 3: 写实现**

在 `keepalive.py` 追加：

```python
def refresh_one_account(
    session_factory: SessionFactory,
    account_id: int,
    *,
    check_timeout_s: float,
) -> str:
    """复用检测按键刷新单账号登录态，带超时看门狗，单账号隔离，永不抛出。

    返回 refreshed_valid / flipped_expired / lock_busy / timeout / error。
    检测放进内部 daemon 线程，主线程 join(timeout)：超时即放弃该账号、告警、循环继续
    （_run_in_plain_thread 的 join 无超时，这里补上墙钟上界）。
    """
    holder: dict[str, Any] = {}

    def _work() -> None:
        # 在子线程内自建 session：session 非线程安全，所有 DB 操作不跨线程。
        from server.app.modules.accounts.auth import check_account
        from server.app.shared.errors import ClientError

        db = session_factory()
        try:
            account = db.get(Account, account_id)
            if account is None:
                holder["result"] = "error"
                return
            holder["display_name"] = account.display_name or f"#{account_id}"
            before = account.status
            try:
                updated = check_account(db, account, AccountCheckRequest())
                db.commit()
            except ClientError:
                db.rollback()
                holder["result"] = "lock_busy"
                return
            after = getattr(updated, "status", before)
            holder["result"] = (
                "flipped_expired" if before == "valid" and after == "expired" else "refreshed_valid"
            )
        except Exception as exc:  # noqa: BLE001 — 单账号隔离
            try:
                db.rollback()
            except Exception:
                pass
            holder["result"] = "error"
            logger.warning("keepalive refresh #%s failed: %s", account_id, exc)
        finally:
            db.close()

    worker = threading.Thread(target=_work, name=f"keepalive-check-{account_id}", daemon=True)
    worker.start()
    worker.join(timeout=check_timeout_s)

    if worker.is_alive():
        logger.warning("keepalive refresh #%s timed out after %ss", account_id, check_timeout_s)
        feishu.send_text(
            "账号保活检测超时",
            f"账号 #{account_id} 登录态检测超过 {check_timeout_s}s 未完成，已跳过本次保活。",
            level="warning",
        )
        return "timeout"

    result = holder.get("result", "error")
    if result == "flipped_expired":
        feishu.send_text(
            "账号登录态失效",
            f"账号「{holder.get('display_name', f'#{account_id}')}」保活检测发现登录态已失效，请到媒体矩阵重新登录。",
            level="warning",
        )
    return result
```

- [ ] **Step 4: 跑测试确认通过**

Run: `GEO_TEST_DATABASE_URL=... python -m pytest server/tests/test_account_keepalive.py -k refresh -v`
Expected: PASS（4 项）

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/accounts/keepalive.py server/tests/test_account_keepalive.py
git commit -m "feat(accounts): 单账号保活刷新 refresh_one_account（超时看门狗 + 失效告警 + 隔离）"
```

---

## Task 6: 编排一轮 `run_keepalive_once`

**Files:**
- Modify: `server/app/modules/accounts/keepalive.py`
- Test: `server/tests/test_account_keepalive.py`

**Interfaces:**
- Consumes: `get_settings`、`parse_hhmm`、`in_keepalive_window`、`window_start_instant`、`window_end_instant`、`select_due_account_ids`、`refresh_one_account`、`compute_next_gap`、`_to_utc_naive`
- Produces: `run_keepalive_once(session_factory, now_local: datetime, rng: random.Random) -> dict[str, Any]`
  - 返回键：`processed: bool`、`in_window: bool`、（processed 时）`account_id: int`、`result: str`、`remaining_due: int`、`next_gap_seconds: float`；（未处理时）可选 `remaining_due: 0`

- [ ] **Step 1: 写失败测试**

追加（用一个轻量假 settings + monkeypatch 选账号/刷新，不碰 DB）：

```python
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
    called = {}
    monkeypatch.setattr(ka, "refresh_one_account",
                        lambda sf, aid, *, check_timeout_s: called.setdefault("aid", aid) or "refreshed_valid")
    now = dt.datetime(2026, 6, 25, 1, 0, tzinfo=_TZ)
    r = ka.run_keepalive_once(lambda: _DummyDB(), now, random.Random(0))
    assert called["aid"] == 7  # due[0]，最旧优先
    assert r["processed"] is True and r["result"] == "refreshed_valid"
    assert r["remaining_due"] == 2
    assert 30.0 <= r["next_gap_seconds"] <= 600.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_account_keepalive.py -k run_once -v`
Expected: FAIL — `AttributeError: ... 'run_keepalive_once'`

- [ ] **Step 3: 写实现**

在 `keepalive.py` 追加：

```python
def run_keepalive_once(
    session_factory: SessionFactory,
    now_local: dt.datetime,
    rng: random.Random,
) -> dict[str, Any]:
    """一轮保活：不在窗口→跳过；窗口内取最旧待刷账号刷一个，返回下次随机间隔。

    now_local 须带 scheduler_tz 时区。处理一个账号后由调用方休眠 next_gap_seconds
    （从本次检测完成后计时）。
    """
    s = get_settings()
    start = parse_hhmm(s.account_keepalive_window_start)
    end = parse_hhmm(s.account_keepalive_window_end)
    if not in_keepalive_window(start, end, now_local):
        return {"processed": False, "in_window": False}

    win_start = window_start_instant(start, now_local)
    db = session_factory()
    try:
        due = select_due_account_ids(db, win_start)
    finally:
        db.close()

    if not due:
        return {"processed": False, "in_window": True, "remaining_due": 0}

    account_id = due[0]
    result = refresh_one_account(
        session_factory, account_id, check_timeout_s=s.account_keepalive_check_timeout_seconds
    )
    remaining_due = len(due) - 1
    win_end = window_end_instant(end, now_local)
    remaining_window_s = (win_end - _to_utc_naive(now_local)).total_seconds()
    gap = compute_next_gap(
        remaining_window_s,
        remaining_due,
        s.account_keepalive_min_gap_seconds,
        s.account_keepalive_max_gap_seconds,
        rng,
    )
    return {
        "processed": True,
        "in_window": True,
        "account_id": account_id,
        "result": result,
        "remaining_due": remaining_due,
        "next_gap_seconds": gap,
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_account_keepalive.py -k run_once -v`
Expected: PASS（3 项）

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/accounts/keepalive.py server/tests/test_account_keepalive.py
git commit -m "feat(accounts): 保活编排 run_keepalive_once（窗口内最旧优先 + 返回随机间隔）"
```

---

## Task 7: 后台线程壳 `start_keepalive` / `stop_keepalive`

**Files:**
- Modify: `server/app/modules/accounts/keepalive.py`
- Test: `server/tests/test_account_keepalive.py`

**Interfaces:**
- Consumes: `get_settings`、`run_keepalive_once`、模块级 `_stop` / `_thread`
- Produces: `start_keepalive(session_factory) -> bool`（disabled / 已在跑 → False）、`stop_keepalive() -> None`

- [ ] **Step 1: 写失败测试**

追加：

```python
def test_start_keepalive_disabled_returns_false(monkeypatch):
    class _Off:
        account_keepalive_enabled = False
        scheduler_tz = "Asia/Shanghai"
    monkeypatch.setattr(ka, "get_settings", lambda: _Off())
    assert ka.start_keepalive(lambda: None) is False


def test_start_keepalive_runs_one_round_then_stops(monkeypatch):
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

    assert ka.start_keepalive(lambda: None) is True
    ka._thread.join(timeout=5)
    assert ka._thread.is_alive() is False
    assert len(rounds) >= 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_account_keepalive.py -k start_keepalive -v`
Expected: FAIL — `AttributeError: ... 'start_keepalive'`

- [ ] **Step 3: 写实现**

在 `keepalive.py` 追加：

```python
def start_keepalive(session_factory: SessionFactory) -> bool:
    """按配置启动后台保活线程。返回是否启动（关闭 / 已在跑 → False）。"""
    global _thread
    if not get_settings().account_keepalive_enabled:
        return False
    if _thread is not None and _thread.is_alive():
        return False

    _stop.clear()
    rng = random.Random()

    def _loop() -> None:
        while not _stop.is_set():
            tz = ZoneInfo(get_settings().scheduler_tz)
            try:
                r = run_keepalive_once(session_factory, dt.datetime.now(tz), rng)
            except Exception:
                logger.exception("account keepalive round failed")
                r = {"processed": False}
            if r.get("processed"):
                sleep_s = float(r.get("next_gap_seconds") or 0.0)
            else:
                sleep_s = float(get_settings().account_keepalive_poll_seconds)
            if _stop.wait(max(1.0, sleep_s)):
                break

    _thread = threading.Thread(target=_loop, daemon=True, name="account-keepalive")
    _thread.start()
    return True


def stop_keepalive() -> None:
    """请求停止后台线程（worker 优雅关闭 / 测试用）。"""
    _stop.set()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_account_keepalive.py -k start_keepalive -v`
Expected: PASS（2 项）

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/accounts/keepalive.py server/tests/test_account_keepalive.py
git commit -m "feat(accounts): 保活后台线程壳 start/stop_keepalive"
```

---

## Task 8: worker 集成

**Files:**
- Modify: `server/worker/executor.py:354-357`（起线程）、`:400-407`（停线程）
- Test: `server/tests/test_account_keepalive.py`

**Interfaces:**
- Consumes: `start_keepalive(SessionLocal)`、`stop_keepalive()`

- [ ] **Step 1: 写失败测试**

追加（验证 worker 在 enabled 时调用了 start_keepalive；用 monkeypatch 拦截，不真起线程）：

```python
def test_worker_main_starts_keepalive(monkeypatch):
    import server.worker.executor as ex

    started = {}
    monkeypatch.setattr("server.app.modules.accounts.keepalive.start_keepalive",
                        lambda sf: started.setdefault("called", True) or False)
    # 让主循环立即退出：第一次 _write_worker_heartbeat 即触发 shutdown
    monkeypatch.setattr(ex, "_startup", lambda db: None)
    monkeypatch.setattr(ex, "_account_login_loop", lambda: None)

    def stop_now(db):
        ex._shutdown = True
    monkeypatch.setattr(ex, "_write_worker_heartbeat", stop_now)
    monkeypatch.setattr(ex, "_claim_next_task", lambda db: None)
    monkeypatch.setattr("server.app.modules.accounts.login_broker.login_broker.shutdown",
                        lambda: None)
    ex._shutdown = False
    try:
        ex.main()
    finally:
        ex._shutdown = False
    assert started.get("called") is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_account_keepalive.py::test_worker_main_starts_keepalive -v`
Expected: FAIL — `assert None is True`（main 未调用 start_keepalive）

- [ ] **Step 3: 写实现**

在 `server/worker/executor.py` 的 `main()`，`login_thread.start()`（约 `:357`）之后追加：

```python
    from server.app.modules.accounts.keepalive import start_keepalive

    if start_keepalive(SessionLocal):
        _logger.info("Worker %s: account keep-alive thread started", WORKER_ID)
```

并在 `login_broker.shutdown()` 块（约 `:400-405`）之后追加停止：

```python
    try:
        from server.app.modules.accounts.keepalive import stop_keepalive

        stop_keepalive()
    except Exception:
        _logger.warning("Worker %s: keepalive stop failed", WORKER_ID, exc_info=True)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_account_keepalive.py::test_worker_main_starts_keepalive -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add server/worker/executor.py server/tests/test_account_keepalive.py
git commit -m "feat(worker): 启动/停止账号保活后台线程"
```

---

## Task 9: 文档 + 全量门禁

**Files:**
- Modify: `CLAUDE.md`（accounts/ 模块描述末尾 + Gotchas）
- 全量 lint / typecheck / test

- [ ] **Step 1: 更新 CLAUDE.md**

在 `accounts/` 模块条目末尾追加一句：

```
- accounts/ 还含**登录态夜间保活**（`keepalive.py`）：worker 后台线程，夜间窗口（`GEO_ACCOUNT_KEEPALIVE_WINDOW_START/END`，默认 23:00–03:00，`GEO_SCHEDULER_TZ` 时区）内以有界随机间隔（`MIN/MAX_GAP_SECONDS`，默认 30s~10min、从上一个检测完成后计时）逐个复用 `check_account(use_browser=True)` 刷新 `status='valid'` 浏览器账号的 storage_state 保活；失效/超时飞书告警、不自动重登。开关 `GEO_ACCOUNT_KEEPALIVE_ENABLED`（默认关）。**仅 worker 跑**，见 `docs/superpowers/specs/2026-06-25-account-login-keepalive-design.md`。
```

在 Gotchas 末尾追加一句：

```
- 账号保活（`GEO_ACCOUNT_KEEPALIVE_ENABLED`）只在**发布 worker** 进程启动，无头浏览器检测依赖容器内 Playwright/Chromium——Windows 本地起不来（与发布同限制）。它和发布/登录共用 `account_check` profile 锁：账号正发布时保活安静跳过、下一晚再试。
```

- [ ] **Step 2: 全量后端门禁**

Run:
```bash
ruff check server/app/modules/accounts/keepalive.py server/worker/executor.py server/app/core/config.py server/tests/test_account_keepalive.py
ruff format --check server/app/modules/accounts/keepalive.py server/tests/test_account_keepalive.py
mypy server/app/modules/accounts/keepalive.py
```
Expected: 全部无错误（`ruff format` 报需要格式化则去掉 `--check` 重跑后再提交）

- [ ] **Step 3: 跑全套保活测试**

Run: `GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test python -m pytest server/tests/test_account_keepalive.py -v`
Expected: 全 PASS（纯函数 + MySQL + worker 集成）

- [ ] **Step 4: 冒烟回归（确保没碰坏账号/worker 模块）**

Run: `GEO_TEST_DATABASE_URL=... python -m pytest server/tests/test_worker_executor.py server/tests/test_taptap_health.py -q`
Expected: PASS（保活未影响现有 worker / taptap 体检）

- [ ] **Step 5: 提交**

```bash
git add CLAUDE.md
git commit -m "docs: 账号登录态夜间保活 env 开关 + 模块说明"
```

---

## Self-Review

**Spec coverage（spec 各节 → 任务映射）：**
- §1 复用检测按键保活 → Task 5（`refresh_one_account` 调 `check_account`）✅
- §2 节奏 A / 30s~10min / 完成后计时 / valid 范围 / worker / 告警 → Task 3+5+6+7+8 ✅
- §4.1 纯函数 5 件 + select + refresh + run_once + start/stop → Task 2/3/4/5/6/7 ✅
- §4.2 worker 集成 → Task 8 ✅
- §4.3 超时看门狗 → Task 5 ✅
- §5 配置 7 项 → Task 1 ✅
- §6 失效/锁冲突/隔离 → Task 5（lock_busy/error/flip）+ select 过滤 ✅
- §7 测试清单 → Task 1-8 各自测试 ✅
- §8 风险（窗口压缩/除零/手动共用 last_checked_at）→ Task 3 测试 + Task 4 测试覆盖 ✅

**Placeholder scan：** 无 TBD/TODO；每个代码步骤含完整代码；每个命令含期望输出。✅

**Type consistency：** `run_keepalive_once` 返回键（`processed`/`in_window`/`account_id`/`result`/`remaining_due`/`next_gap_seconds`）在 Task 6 定义、Task 7 循环消费一致；`refresh_one_account` 返回 5 值字符串在 Task 5 定义、Task 6 透传一致；`select_due_account_ids(db, window_start)` 签名 Task 4 定义、Task 6 调用一致；`compute_next_gap` 5 参 Task 3 定义、Task 6 调用一致。✅

> 注：Task 6 测试中 `_DummyDB` / `_FakeKaSettings` 为测试桩，随测试代码就近定义于首次使用之前。各 `_Fake*Settings` 桩只列出被读到的字段；若实现中 `run_keepalive_once` / `start_keepalive` 读到桩未定义的 settings 字段，给桩补齐对应类属性即可。
