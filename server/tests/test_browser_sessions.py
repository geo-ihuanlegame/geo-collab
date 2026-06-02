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
