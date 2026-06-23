"""平台发布驱动注册表。

每个平台驱动实现 PlatformDriver Protocol，在模块 import 时调 register(...) 注册；
main.py 顶部按需 import 各驱动文件触发注册。同一平台可注册多个变体（variant），
由 resolve_driver() 按环境变量 GEO_<PLATFORM>_DRIVER 选择，便于灰度与回滚。
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

from playwright.sync_api import BrowserContext, Page

from server.app.modules.tasks.drivers.base import PublishPayload, PublishResult


@runtime_checkable
class PlatformDriver(Protocol):
    code: str
    name: str
    home_url: str
    publish_url: str

    def detect_logged_in(self, *, url: str, title: str, body: str) -> bool:
        """当前页面表明用户已登录时返回 True。"""

    def publish(
        self,
        *,
        page: Page,
        context: BrowserContext,
        payload: PublishPayload,
        stop_before_publish: bool,
        commit_guard=None,
        retry_policy=None,
    ) -> PublishResult:
        """填写表单、上传资源并点击发布；不负责浏览器生命周期。
        commit_guard/retry_policy 可选：不接入弹性的驱动忽略即可。"""

    # ── 平台侧用户 ID 抽取（查重 / 共享账号，见设计稿 §3）────────────────────────
    #
    # 两个方法都**可选**（Protocol 默认实现返回 None）：缺省 = 不抽取 = platform_user_id
    # 保持 NULL = 不查重，对其它平台**无回归**。实现者各自做 sync / async Playwright I/O，
    # 解析复用纯函数层。best-effort：任何异常都吞掉返回 None，绝不抛出拖垮登录 / 检测。
    #
    # sync 版用于账号有效性检测路径（auth._check_account_in_browser，sync Playwright）；
    # async 版用于登录 broker（login_broker，async Playwright 活页）。

    def extract_platform_user_id_sync(self, *, page: Page) -> str | None:
        """同步 Playwright 活页上抽取平台侧用户 ID；不支持 / 失败返回 None。"""
        return None

    async def extract_platform_user_id_async(self, *, page: Page) -> str | None:
        """异步 Playwright 活页上抽取平台侧用户 ID；不支持 / 失败返回 None。"""
        return None


logger = logging.getLogger(__name__)

_REGISTRY: dict[str, PlatformDriver] = {}


def register(driver: PlatformDriver) -> None:
    """注册平台默认驱动；同一 code 重复注册直接报错（防止覆盖）。"""
    if driver.code in _REGISTRY:
        raise ValueError(f"Driver already registered: {driver.code}")
    _REGISTRY[driver.code] = driver


def get_driver(platform_code: str) -> PlatformDriver:
    if platform_code not in _REGISTRY:
        raise ValueError(f"Unknown platform: {platform_code}")
    return _REGISTRY[platform_code]


def all_driver_codes() -> list[str]:
    return sorted(_REGISTRY.keys())


_VARIANTS: dict[tuple[str, str], PlatformDriver] = {}


def register_variant(
    platform_code: str, variant: str, driver: PlatformDriver, *, replace: bool = False
) -> None:
    """注册某平台的命名变体驱动；replace=False 时重复注册报错。

    变体不进默认注册表，只能由 resolve_driver() 经 GEO_<PLATFORM>_DRIVER 选中。
    """
    key = (platform_code, variant)
    if key in _VARIANTS and not replace:
        raise ValueError(f"Driver variant already registered: {platform_code}/{variant}")
    _VARIANTS[key] = driver


def resolve_driver(platform_code: str) -> PlatformDriver:
    """按 GEO_<PLATFORM>_DRIVER 选择驱动；未配置则回退到默认注册表。"""
    variant = os.environ.get(f"GEO_{platform_code.upper()}_DRIVER", "").strip()
    if variant:
        chosen = _VARIANTS.get((platform_code, variant))
        if chosen is not None:
            return chosen
        logger.warning(
            "GEO_%s_DRIVER=%r is set but no such variant is registered; using default driver",
            platform_code.upper(),
            variant,
        )
    return get_driver(platform_code)


def is_api_driver(platform_code: str) -> bool:
    """Return whether the default driver publishes through a server-side API path."""
    driver = _REGISTRY.get(platform_code)
    return getattr(driver, "mode", "browser") == "api"


def is_driver_registered(platform_code: str) -> bool:
    """该 platform_code 是否在**本进程**注册了默认驱动。

    注册是 import 副作用、按进程隔离的（见 drivers/bootstrap.py）：某进程漏 import 某驱动时，
    is_api_driver 会静默把它当浏览器驱动。调度层据此显式报错，而非悄悄走错路径。
    """
    return platform_code in _REGISTRY
