from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from server.app.modules.articles.parser import BodySegment  # noqa: F401


@dataclass(frozen=True)
class PublishPayload:
    """Fully-resolved article data passed to platform drivers.

    All asset paths are pre-resolved from the DB before launching the browser,
    so drivers never need to access ORM relationships or call resolve_asset_path.
    """

    title: str
    cover_asset_path: Path
    body_segments: list[BodySegment]
    account_key: str
    state_path: Path
    display_name: str
    platform_code: str
    temp_files: tuple[Path, ...] = ()


@dataclass(frozen=True)
class PublishResult:
    url: str | None
    title: str
    message: str


class PublishError(Exception):
    """Platform-neutral publish failure with an optional diagnostic screenshot."""

    def __init__(self, message: str, screenshot: bytes | None = None):
        super().__init__(message)
        self.screenshot = screenshot


class UserInputRequired(PublishError):
    """Raised when publishing must pause for login, captcha, or similar input."""

    def __init__(
        self,
        message: str,
        screenshot: bytes | None = None,
        session_id: str | None = None,
        novnc_url: str | None = None,
        error_type: str = "login_required",
    ):
        super().__init__(message, screenshot)
        self.session_id = session_id
        self.novnc_url = novnc_url
        self.error_type = error_type
