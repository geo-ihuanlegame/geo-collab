from pathlib import Path

from server.app.core.config import get_settings
from server.app.modules.accounts import browser as browser_session

browser_sessions = browser_session


class FakeProcess:
    def __init__(self, command):
        self.command = command
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def test_remote_browser_session_starts_processes_and_cleans_up(monkeypatch, tmp_path: Path):
    started: list[FakeProcess] = []

    def fake_popen(command, stdout=None, stderr=None):
        process = FakeProcess(command)
        started.append(process)
        return process

    monkeypatch.setenv("GEO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GEO_PUBLISH_REMOTE_BROWSER_HOST", "127.0.0.1")
    monkeypatch.setenv("GEO_PUBLISH_NOVNC_WEB_DIR", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(browser_session.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(browser_session.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(browser_session, "_wait_for_x_display", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_session, "_wait_for_port", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_session, "_port_available", lambda *_args, **_kwargs: True)

    try:
        with browser_sessions.managed_remote_browser_session("spike") as session:
            assert session is not None
            assert session.display == ":99"
            assert session.vnc_port == 5900
            assert session.novnc_port == 6080
            assert session.novnc_url.startswith("http://127.0.0.1/novnc/vnc.html")
            assert (
                "path=novnc%2Fws%2F6080" in session.novnc_url
                or "path=novnc/ws/6080" in session.novnc_url
            )
            assert len(browser_sessions.active_remote_browser_sessions()) == 1

        assert len(started) == 3
        assert "Xvfb" in started[0].command[0]
        assert "x11vnc" in started[1].command[0]
        assert "websockify" in started[2].command[0]
        assert all(process.terminated for process in started)
        assert browser_sessions.active_remote_browser_sessions() == []
    finally:
        browser_sessions._stop_idle_cleanup()
        get_settings.cache_clear()


def test_start_remote_browser_session_displayless_skips_processes(monkeypatch, tmp_path: Path):
    """with_display=False：不起任何子进程，会话无 display/novnc，但可注册与停止。"""
    monkeypatch.setenv("GEO_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()

    def _boom_popen(*_a, **_k):
        raise AssertionError("Popen must not be called for a displayless session")

    monkeypatch.setattr(browser_session.subprocess, "Popen", _boom_popen)
    monkeypatch.setattr(browser_session, "_write_session_to_db", lambda *a, **k: None)
    monkeypatch.setattr(browser_session, "_delete_session_from_db", lambda *a, **k: None)
    monkeypatch.setattr(browser_session, "_start_idle_cleanup", lambda: None)
    browser_session._reset_globals()
    try:
        session = browser_session.start_remote_browser_session(
            "acct1", platform_code="toutiao", with_display=False
        )
        assert session.processes == []
        assert session.display == ""
        assert session.novnc_url == ""
        assert session.id in {s.id for s in browser_session.active_remote_browser_sessions()}

        browser_session.stop_remote_browser_session(session.id)
        assert browser_session.active_remote_browser_sessions() == []
    finally:
        browser_session._reset_globals()
        get_settings.cache_clear()


def test_get_or_create_account_session_passes_with_display(monkeypatch):
    """get_or_create_account_session 把 with_display 透传给 start_remote_browser_session。"""
    import types

    captured = {}

    def fake_start(account_key, platform_code="", profile_key=None, *, with_display=True):
        captured["with_display"] = with_display
        session = types.SimpleNamespace(id="s1", browser_context=None)
        browser_session._active_sessions["s1"] = session
        return session

    monkeypatch.setattr(browser_session, "start_remote_browser_session", fake_start)
    browser_session._reset_globals()
    try:
        browser_session.get_or_create_account_session("toutiao", "k1", with_display=False)
        assert captured["with_display"] is False
    finally:
        browser_session._reset_globals()
