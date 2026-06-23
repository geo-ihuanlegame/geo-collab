"""平台驱动的共享数据契约：传给驱动的 PublishPayload、返回的 PublishResult，
以及驱动级异常 PublishError / UserInputRequired。

驱动只拿这里的纯数据结构，所有 asset 路径在进浏览器前已从 DB 预解析，
驱动内不碰 ORM（见 CLAUDE.md「PlatformDriver」约束）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from server.app.modules.articles.parser import BodySegment  # noqa: F401


@dataclass(frozen=True)
class PublishPayload:
    """传给平台驱动的已完整解析文章数据。

    所有 asset 路径都会在启动浏览器前从 DB 预解析，因此驱动不需要访问 ORM
    关系，也不需要调用 resolve_asset_path。
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
class ApiPublishPayload:
    """API 型平台驱动的发布载荷：纯数据，含已就绪的 access_token，不含 secret。

    与 PublishPayload 的区别：无 state_path/account_key（无浏览器态）；cover_path 可空
    （驱动内回落正文首图）；token 由 runner_api 从 DB 缓存解析后注入。
    """

    title: str
    body_segments: list[BodySegment]
    cover_path: Path | None
    display_name: str
    platform_code: str
    access_token: str
    temp_files: tuple[Path, ...] = ()


@dataclass(frozen=True)
class PublishResult:
    url: str | None
    title: str
    message: str


class PublishError(Exception):
    """平台无关的发布失败异常，可附带诊断截图。"""

    def __init__(self, message: str, screenshot: bytes | None = None):
        super().__init__(message)
        self.screenshot = screenshot


class UserInputRequired(PublishError):
    """需要 noVNC 人工接管（登录失效 / 验证码等）时抛出。

    携带 session_id / novnc_url 供前端接管；error_type 区分接管原因。
    注意：stop_before_publish=True 的正常停顿不抛此异常（见 CLAUDE.md）。
    """

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


class CommitUncertainError(PublishError):
    """跨提交点后发生网络失败：请求已发出、平台是否受理未知。绝不自动重发（at-most-once）。"""


def _walk_exc(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


def _commit_is_clean_failure(exc: BaseException) -> bool:
    """True = 有正面证据平台未受理（干净失败，原异常透出，可安全重试）。
    False = 结果未知，包成 CommitUncertainError。at-most-once 默认保守取 False。"""
    # 业务级拒绝：服务端回了非空错误码 → 必定未建记录
    if getattr(exc, "errcode", None) is not None:
        return True
    for e in _walk_exc(exc):
        mod = type(e).__module__ or ""
        name = type(e).__name__
        if mod.startswith("httpx"):
            if name in {"ConnectError", "ConnectTimeout"}:
                return True  # 连接从未建立 → 请求从未发出
            if name in {
                "ReadTimeout",
                "WriteTimeout",
                "PoolTimeout",
                "RemoteProtocolError",
                "ReadError",
                "WriteError",
                "NetworkError",
            }:
                return False  # 已发出或可能已发出 → 未知
    return False


class CommitGuard:
    """把不可逆提交包进 committing()。进入时落 commit_attempted_at（经 runner 注入的回调，
    驱动不碰 ORM）；退出时按异常性质分流为干净失败 or CommitUncertainError。"""

    def __init__(self, mark_pending: Callable[[], None]):
        self._mark_pending = mark_pending

    @contextmanager
    def committing(self) -> Iterator[None]:
        self._mark_pending()
        try:
            yield
        except CommitUncertainError:
            raise
        except BaseException as exc:  # noqa: BLE001
            if _commit_is_clean_failure(exc):
                raise
            raise CommitUncertainError(
                f"提交后网络中断，平台受理结果未知: {exc}",
                screenshot=getattr(exc, "screenshot", None),
            ) from exc


NOOP_COMMIT_GUARD = CommitGuard(mark_pending=lambda: None)
