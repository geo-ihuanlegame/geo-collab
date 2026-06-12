"""回归：发布 worker 与 web 进程必须注册全部平台驱动（含 wechat_mp 这类 API 驱动）。

漏注册 wechat_mp 会让 worker 进程里 ``is_api_driver("wechat_mp")`` 返回 False，把公众号账号
误判成浏览器发布、抛 "浏览器发布需要 storage_state，该账号为 API 接入"。驱动注册集中在
``server/app/modules/tasks/drivers/bootstrap.py``，main.py 与 worker 共用，本测试守护其完整性。

放在**全新子进程**里跑：同一 pytest 进程内别的用例可能已经 import 过各驱动（如 create_app），
污染全局 ``_REGISTRY``，导致 in-process 断言永远为真、测不出 bootstrap 漏项的回归。
驱动包是纯逻辑（不碰 ORM / DB），子进程只 import 驱动即可，无需数据库环境。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_drivers_bootstrap_registers_all_platforms() -> None:
    code = (
        "import server.app.modules.tasks.drivers.bootstrap\n"
        "from server.app.modules.tasks.drivers import is_api_driver, all_driver_codes\n"
        "codes = set(all_driver_codes())\n"
        "assert 'toutiao' in codes, codes\n"
        "assert 'wechat_mp' in codes, codes\n"
        "assert is_api_driver('wechat_mp') is True\n"
        "assert is_api_driver('toutiao') is False\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "OK" in result.stdout


def test_build_runner_raises_on_unregistered_platform() -> None:
    """调度层防御：platform.code 未注册驱动时显式抛 PublishError，不静默回退浏览器发布。

    没有这道兜底，未注册的 API 平台会落到 run_publish 的 state_path 检查、报误导性的
    「浏览器发布需要 storage_state」，把真正的「驱动漏注册」根因藏起来。
    """
    from server.app.modules.tasks.drivers.base import PublishError
    from server.app.modules.tasks.executor import build_publish_runner_for_record

    fake_record = SimpleNamespace(
        platform=SimpleNamespace(code="totally_unregistered_platform"),
        id=1,
    )
    with pytest.raises(PublishError, match="未在本进程注册"):
        build_publish_runner_for_record(fake_record)
