"""Task 9（封堵 #3）：display/port 泄漏台账 + 对账回收 —— 号段记账纯逻辑单测。

根因：`_stop_session_processes` 对 SIGKILL 后仍不退的进程只 `logger.error`，会话已从
`_active_sessions` 摘除 → 其 display/vnc/novnc 号段既不在「占用」集合、底层 zombie 又仍占着真实
socket/端口；号段（base..base+1000）泄漏满后 `start_remote_browser_session` 抛错、全站发布瘫痪。

契约（纯逻辑、假进程、无 DB / 无真子进程）：
- `_stop_session_processes` 返回**未能杀死**的进程（survivors），可杀的返回空。
- 注册泄漏会话后，其号段被 `_reserve_numbers` 视为「占用」——绝不复用 zombie 仍占的号。
- `reconcile_leaked_sessions`：进程确认已死→回收号段（出账、关日志句柄、清 X11 socket）；仍存活→
  重试强杀、留账。

容器内真实 Xvfb 泄漏冒烟＝补充（不可在 Windows/CI 纯逻辑复现），见 plan Task 9 Step 3。
注意：顶层 import 仅 browser / config / 标准库（collection 安全）；resource_metrics 在函数内 lazy import。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from server.app.core.config import get_settings
from server.app.modules.accounts import browser


class _FakeProc:
    """假子进程：poll/terminate/kill/wait 行为可控，模拟「SIGTERM 死 / SIGKILL 死 / 怎么都不死」。"""

    def __init__(self, *, die_on: str | None = None, pid: int = 999) -> None:
        # die_on ∈ {None=永不死, "terminate", "kill"}
        self._alive = True
        self._die_on = die_on
        self.pid = pid
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self._die_on == "terminate":
            self._alive = False

    def kill(self) -> None:
        self.kill_calls += 1
        if self._die_on == "kill":
            self._alive = False

    def wait(self, timeout=None):
        if self._alive:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        return 0


class _FakeLog:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _managed(proc: _FakeProc) -> browser.ManagedProcess:
    return browser.ManagedProcess(name="xvfb", process=proc, log_handle=_FakeLog())


def _session(*, display=777, vnc=5950, novnc=6150, procs=None) -> browser.RemoteBrowserSession:
    return browser.RemoteBrowserSession(
        id="sess-1",
        account_key="acct",
        display_number=display,
        display=f":{display}",
        vnc_port=vnc,
        novnc_port=novnc,
        novnc_url="http://x",
        log_dir=Path("."),
        processes=list(procs or []),
    )


def test_stop_returns_survivors_for_unkillable_processes():
    """`_stop_session_processes` 返回未能杀死的进程；可杀的进程日志句柄被关、不在返回里。"""
    browser._reset_globals()
    killable = _managed(_FakeProc(die_on="kill"))
    stuck = _managed(_FakeProc(die_on=None))
    session = _session(procs=[killable, stuck])

    survivors = browser._stop_session_processes(session)

    assert [m.process for m in survivors] == [stuck.process]  # 只剩怎么都不死的那个
    assert killable.log_handle.closed is True  # 已死的句柄被关
    assert stuck.process.kill_calls >= 1  # 对卡死进程确实尝试过强杀


def test_all_killable_yields_no_survivors():
    browser._reset_globals()
    a = _managed(_FakeProc(die_on="terminate"))
    b = _managed(_FakeProc(die_on="kill"))
    session = _session(procs=[a, b])

    assert browser._stop_session_processes(session) == []
    assert a.log_handle.closed is True
    assert b.log_handle.closed is True


def test_leaked_numbers_block_reuse(monkeypatch):
    """注册泄漏会话后，其 display/vnc/novnc 号段被 `_reserve_numbers` 视为占用、不再发出。"""
    from server.app.shared import resource_metrics as rm

    browser._reset_globals()
    monkeypatch.setattr(rm, "_alert_hook", lambda *a, **k: None)

    settings = get_settings()
    d0 = settings.publish_remote_browser_display_base
    v0 = settings.publish_remote_browser_vnc_base_port
    n0 = settings.publish_remote_browser_novnc_base_port

    leaked = _session(display=d0, vnc=v0, novnc=n0)
    browser._register_leaked_session(leaked, [_managed(_FakeProc(die_on=None))])

    display, vnc, novnc = browser._reserve_numbers()

    assert display != d0
    assert vnc != v0
    assert novnc != n0


def test_reconcile_reclaims_when_process_dead(monkeypatch):
    """对账时进程确认已死：出账（号段回收）、关日志句柄，返回回收条数。"""
    from server.app.shared import resource_metrics as rm

    browser._reset_globals()
    monkeypatch.setattr(rm, "_alert_hook", lambda *a, **k: None)

    session = _session(display=800, vnc=5951, novnc=6151)
    mp = _managed(_FakeProc(die_on=None))
    browser._register_leaked_session(session, [mp])
    assert session.id in browser._leaked_sessions

    reclaimed = browser.reconcile_leaked_sessions(is_alive=lambda m: False)

    assert reclaimed == 1
    assert session.id not in browser._leaked_sessions
    assert mp.log_handle.closed is True


def test_reconcile_keeps_and_retries_when_process_alive(monkeypatch):
    """对账时进程仍存活：重试强杀、留账、号段不回收。"""
    from server.app.shared import resource_metrics as rm

    browser._reset_globals()
    monkeypatch.setattr(rm, "_alert_hook", lambda *a, **k: None)

    session = _session(display=801, vnc=5952, novnc=6152)
    proc = _FakeProc(die_on=None)
    browser._register_leaked_session(session, [_managed(proc)])

    reclaimed = browser.reconcile_leaked_sessions(is_alive=lambda m: True)

    assert reclaimed == 0
    assert session.id in browser._leaked_sessions
    assert proc.kill_calls >= 1  # 重试过强杀
