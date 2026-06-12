"""锁定 worker 入口的驱动注册行为。

生产 worker 与 Web 应用分属不同进程（`python -m server.worker.executor`）。
它必须同时注册默认的头条 DOM 驱动和页内变体，确保
`GEO_TOUTIAO_DRIVER=inpage` 在 worker 进程里确实解析到页内驱动。

这些测试会启动全新的子进程，只导入 `server.worker.executor`（不调用
`main()`）。如果在当前测试进程内执行，会被其它测试导入污染，例如
conftest 导入应用后已经注册了变体，导致朴素断言误通过。子进程能保证
真实的修复前失败 / 修复后通过。

导入 worker 模块不需要真实数据库连接（`create_engine` 是惰性的），但确实
需要设置 `GEO_DATA_DIR` 和 `GEO_DATABASE_URL`，所以子进程环境提供一次性数据
目录和未实际使用的 MySQL URL。
"""

from __future__ import annotations

import subprocess
import sys
import tempfile

# 在全新解释器中运行的脚本：只导入 worker 入口（模块级代码，不调用 main()），
# 然后要求驱动注册表解析头条驱动，并打印解析出的类名。若没有注册驱动，
# `resolve_driver` 会抛异常；这里打印异常，确保断言明确失败，而不是子进程静默崩溃。
_SUBPROCESS_SCRIPT = """
import server.worker.executor  # noqa: F401  （导入时必须注册驱动）

from server.app.modules.tasks.drivers import resolve_driver

try:
    print(type(resolve_driver("toutiao")).__name__)
except Exception as exc:  # pragma: no cover - 仅在 RED 路径会走到
    print(f"RESOLVE_ERROR: {type(exc).__name__}: {exc}")
"""


def _run_worker_subprocess(driver_env: str | None) -> str:
    """在全新进程中导入 worker 入口，返回解析出的头条驱动类名或错误标记。"""
    with tempfile.TemporaryDirectory() as data_dir:
        env = {
            "GEO_DATA_DIR": data_dir,
            "GEO_DATABASE_URL": "mysql+pymysql://u:p@127.0.0.1:3306/geo_unused_test",
            "GEO_JWT_SECRET": "test-secret-not-used-here",
        }
        if driver_env is not None:
            env["GEO_TOUTIAO_DRIVER"] = driver_env

        # 继承当前进程的 PATH 等环境，保证解释器和 site-packages 能正确解析；
        # 再在其上覆盖本测试所需配置。
        import os

        full_env = {**os.environ, **env}
        if driver_env is None:
            full_env.pop("GEO_TOUTIAO_DRIVER", None)

        result = subprocess.run(
            [sys.executable, "-c", _SUBPROCESS_SCRIPT],
            capture_output=True,
            text=True,
            env=full_env,
            timeout=120,
        )
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout.strip().splitlines()[-1]


def test_worker_import_registers_inpage_variant():
    """设置 GEO_TOUTIAO_DRIVER=inpage 时，worker 进程必须解析到页内驱动。"""
    resolved = _run_worker_subprocess("inpage")
    assert resolved == "ToutiaoInPageDriver", (
        f"expected ToutiaoInPageDriver, got {resolved!r}; "
        "the worker entrypoint did not register the in-page variant"
    )


def test_worker_import_registers_default_driver():
    """未设置 GEO_TOUTIAO_DRIVER 时，worker 进程必须解析到默认 DOM 驱动。"""
    resolved = _run_worker_subprocess(None)
    assert resolved == "ToutiaoDriver", (
        f"expected ToutiaoDriver, got {resolved!r}; "
        "the worker entrypoint did not register the default driver"
    )
