"""Regression test: MCP server must register tools when invoked via the correct entry point.

The bug: `python -m server.mcp.server` triggers Python's __main__ vs package-module dual-import
behavior. server.py is loaded twice (once as `__main__`, once as `server.mcp.server`), so
`mcp = FastMCP("geo")` runs twice and creates two FastMCP instances. @mcp.tool() decorators
in tools/*.py register against one instance; mcp.run() runs on the other (which has 0 tools).
Result: stdio tools/list returns []. Fix: invoke via `python -m server.mcp`, which uses
__main__.py as a shim and keeps server.mcp.server as a single canonical module.

This test spawns the actual MCP server via `docker compose exec` and drives a minimal
stdio handshake (initialize → notifications/initialized → tools/list). It asserts the
tools list is non-empty and contains expected names. Marked @pytest.mark.mysql because
it requires the dev container to be running.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.dev.yml"


def _docker_available() -> bool:
    """Returns True only when the dev compose stack's `app` service is currently up.

    Just having a docker binary + the compose file isn't enough — CI runners
    have docker installed but don't bring up our dev stack, so `docker compose
    exec app ...` fails with missing-env errors (MYSQL_ROOT_PASSWORD, etc.).
    We confirm the actual `app` container is running before trying to exec.
    """
    if shutil.which("docker") is None:
        return False
    if not COMPOSE_FILE.exists():
        return False
    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "ps",
                "--status=running",
                "--services",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if result.returncode != 0:
        return False
    running_services = set(result.stdout.split())
    return "app" in running_services


@pytest.mark.mysql
@pytest.mark.skipif(not _docker_available(), reason="docker / docker-compose.dev.yml not available")
def test_python_dash_m_server_mcp_registers_tools() -> None:
    """Spawn `python -m server.mcp` in dev container and verify tools/list returns >0 tools."""
    proc = subprocess.Popen(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "exec",
            "-T",
            "-e",
            "GEO_MCP_TOKEN=test-mcp-entry",
            "-e",
            "GEO_API_BASE_URL=http://127.0.0.1:8000",
            "app",
            "python",
            "-m",
            "server.mcp",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
    )
    try:
        out_lines: list[str] = []

        def reader() -> None:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                out_lines.append(raw.decode("utf-8", "replace"))

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        def send(obj: dict) -> None:
            assert proc.stdin is not None
            proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            proc.stdin.flush()

        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        )
        time.sleep(1.5)
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        time.sleep(0.3)
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})

        deadline = time.time() + 8.0
        tools_payload: dict | None = None
        while time.time() < deadline and tools_payload is None:
            for line in list(out_lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == 2 and "result" in msg:
                    tools_payload = msg["result"]
                    break
            if tools_payload is None:
                time.sleep(0.2)
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            proc.kill()

    assert tools_payload is not None, f"no tools/list response; raw stdout:\n{''.join(out_lines)}"
    tools = tools_payload.get("tools") or []
    names = sorted(t.get("name") for t in tools)
    assert len(names) > 0, (
        "tools/list returned empty array — likely the __main__ vs package dual-import bug. "
        "Did someone change the entry point back to `python -m server.mcp.server`?"
    )
    expected_subset = {"list_articles", "save_article", "score_recent_articles"}
    missing = expected_subset - set(names)
    assert not missing, f"expected tools missing: {missing}; got {names}"


@pytest.mark.mysql
@pytest.mark.skipif(not _docker_available(), reason="docker / docker-compose.dev.yml not available")
def test_old_entry_point_raises_defensive_error() -> None:
    """The defensive assertion in main() must trip when invoked via the buggy `python -m server.mcp.server`."""
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "exec",
            "-T",
            "-e",
            "GEO_MCP_TOKEN=test-defensive",
            "-e",
            "GEO_API_BASE_URL=http://127.0.0.1:8000",
            "app",
            "python",
            "-m",
            "server.mcp.server",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(REPO_ROOT),
    )
    combined = (result.stdout or "") + (result.stderr or "")
    assert result.returncode != 0, (
        f"buggy entry point should fail, but exited 0; output:\n{combined}"
    )
    assert "0 registered tools" in combined or "dual-import" in combined, (
        f"expected the defensive RuntimeError message; got:\n{combined}"
    )


if os.environ.get("RUN_MCP_ENTRY_TEST_STANDALONE") == "1":  # manual smoke
    test_python_dash_m_server_mcp_registers_tools()
    print("OK")
