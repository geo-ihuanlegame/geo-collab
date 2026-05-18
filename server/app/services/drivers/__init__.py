from __future__ import annotations

from typing import Protocol, runtime_checkable

from playwright.sync_api import BrowserContext, Page

from server.app.modules.tasks.drivers.driver_Base import PublishPayload, PublishResult


@runtime_checkable
class PlatformDriver(Protocol):
    code: str        # matches Platform.code, e.g. "toutiao"
    name: str        # display name, e.g. "头条号"
    home_url: str    # used for login state detection
    publish_url: str # publishing page URL

    def detect_logged_in(self, *, url: str, title: str, body: str) -> bool:
        """Return True if the current page indicates the user is logged in."""

    def publish(
        self,
        *,
        page: Page,
        context: BrowserContext,
        payload: PublishPayload,
        stop_before_publish: bool,
    ) -> PublishResult:
        """Fill form, upload assets, click publish. Does not manage browser lifecycle.

        Raises UserInputRequired when login/captcha is needed.
        """


_REGISTRY: dict[str, PlatformDriver] = {}


def register(driver: PlatformDriver) -> None:
    if driver.code in _REGISTRY:
        raise ValueError(f"Driver already registered: {driver.code}")
    _REGISTRY[driver.code] = driver


def get_driver(platform_code: str) -> PlatformDriver:
    if platform_code not in _REGISTRY:
        raise ValueError(f"Unknown platform: {platform_code}")
    return _REGISTRY[platform_code]


def all_driver_codes() -> list[str]:
    return sorted(_REGISTRY.keys())
