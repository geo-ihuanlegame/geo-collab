"""登录浏览器 broker：用 async Playwright 在单个事件循环上并发管理多个登录会话。

为什么需要它：同步 Playwright 的「活的 context」⟺「该线程上有个一直 running 的事件
循环」绑死，一个线程上没法并存两个活的 sync Playwright 实例。登录会话要保持浏览器开着
等用户扫码（长存活），多个账号并发登录时，旧的「单线程顺序处理」会在第二个会话
`sync_playwright().start()` 时撞上前一个还 running 的 loop，抛
"Playwright Sync API inside the asyncio loop"。

本 broker 改用 **async** Playwright，把所有登录浏览器的句柄放到**同一个**常驻事件循环
（独立 daemon 线程 `login-browser-broker`）上——一个 loop 挂 N 个 context 正是 async API
的设计用法，天然并发、无线程绑定问题、不会触发上面的同步保护。worker 轮询线程 / FastAPI
线程池通过 `run_coroutine_threadsafe` 把协程投递到这个 loop，并发上限 + 超时取消兜底。

实际 Playwright 调用都收敛在模块级 `_pw_*` 协程里（可被测试 monkeypatch），所以 broker 的
并发 / 上限 / 超时行为能在没有浏览器的机器上单测。
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.app.shared.errors import ClientError

_logger = logging.getLogger(__name__)

LAUNCH_TIMEOUT_SECONDS = 60.0
READ_STATE_TIMEOUT_SECONDS = 30.0
CLOSE_TIMEOUT_SECONDS = 20.0
_DEFAULT_MAX_CONCURRENT = 8


@dataclass(frozen=True)
class LoginBrowserResult:
    logged_in: bool
    url: str
    title: str
    # 平台侧用户 ID（creator-ID 查重，见设计稿 §3/§4）。未传抽取器 / 抽取失败时为 None。
    extracted_platform_user_id: str | None = None


# 异步抽取器：在 broker 自己的事件循环上、对 live async page 运行，返回平台侧用户 ID 或 None。
# best-effort——抽取器内部已自吞异常，broker 这层再加一层兜底，绝不让抽取拖垮登录读态。
ExtractorAsync = Callable[..., Awaitable[str | None]]


# ── 实际 Playwright 调用（测试用 monkeypatch 替换这些 seam，无需真浏览器）──────────


async def _pw_open(
    profile_dir: Path, options: dict[str, Any], display: str
) -> tuple[Any, Any, Any]:
    """起 async Playwright + 持久化 context，返回 (playwright, context, page)。

    失败时清掉已建的半成品句柄再抛。懒导入 playwright，使本模块在没装 playwright 时也能 import。
    """
    from playwright.async_api import async_playwright

    launch_opts = dict(options)
    launch_opts["env"] = {**os.environ, "DISPLAY": display}
    pw = await async_playwright().start()
    context = None
    try:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir), **launch_opts
        )
        context.set_default_navigation_timeout(30000)
        page = await _primary_page(context)
        return pw, context, page
    except BaseException:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        try:
            await pw.stop()
        except Exception:
            pass
        raise


async def _primary_page(context: Any) -> Any:
    pages = list(getattr(context, "pages", []) or [])
    if pages:
        page = pages[0]
        for extra in pages[1:]:
            try:
                await extra.close()
            except Exception:
                pass
        return page
    return await context.new_page()


async def _pw_goto(page: Any, home_url: str) -> None:
    await page.goto(home_url, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        _logger.warning("login page did not reach networkidle: %s", home_url, exc_info=True)


async def _pw_read(context: Any, page: Any) -> tuple[str, str, str]:
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    url = page.url
    title = ""
    body = ""
    try:
        title = await page.title()
        body = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        _logger.warning("login state read failed", exc_info=True)
    return url, title, body


async def _pw_storage_state(context: Any, state_path: Path) -> None:
    await context.storage_state(path=str(state_path))


async def _pw_close(pw: Any, context: Any) -> None:
    try:
        await context.close()
    except Exception:
        pass
    try:
        await pw.stop()
    except Exception:
        pass


# ── broker ─────────────────────────────────────────────────────────────────────


class LoginBrowserBroker:
    """进程内单例：一个常驻事件循环线程，承载所有登录浏览器的 async Playwright 句柄。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # session_id -> (playwright, context, page)；只在 loop 线程上读写
        self._contexts: dict[str, tuple[Any, Any, Any]] = {}

    @property
    def _cap(self) -> int:
        try:
            from server.app.core.config import get_settings

            return max(1, int(get_settings().login_max_concurrent_browsers))
        except Exception:
            return _DEFAULT_MAX_CONCURRENT

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop

            loop = asyncio.new_event_loop()

            def _run() -> None:
                asyncio.set_event_loop(loop)
                loop.run_forever()

            thread = threading.Thread(target=_run, name="login-browser-broker", daemon=True)
            thread.start()
            self._loop = loop
            self._thread = thread
            return loop

    def _submit(self, coro: Any, timeout: float) -> Any:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError:
            future.cancel()  # 让 loop 上的协程收到 CancelledError，自行清理半成品句柄
            raise

    # ── facade（任意线程可调）────────────────────────────────────────────────

    def launch_login_browser(
        self, session_id: str, *, profile_dir: Path, options: dict[str, Any], display: str
    ) -> None:
        self._submit(
            self._launch(session_id, profile_dir, options, display), LAUNCH_TIMEOUT_SECONDS
        )

    def load_login_page(self, session_id: str, home_url: str) -> None:
        """best-effort 把登录页 goto 到 home_url；失败只记日志、不抛（与历史行为一致）。"""
        try:
            self._submit(self._load_page(session_id, home_url), LAUNCH_TIMEOUT_SECONDS)
        except Exception:
            _logger.warning("login page load failed for %s", session_id, exc_info=True)

    def read_login_state(
        self,
        session_id: str,
        *,
        detect: Callable[[str, str, str], bool],
        state_path: Path,
        extractor: ExtractorAsync | None = None,
    ) -> LoginBrowserResult:
        """读登录态并存盘；若已登录且传了 extractor，在 live async page 上跑它抽 creator-ID。

        extractor 在 broker 自己的事件循环线程上运行，正是 async Playwright 活页所在处
        （见设计稿 §4：抽取必须在持有活页的进程 / loop 上跑）。
        """
        return self._submit(
            self._read(session_id, detect, state_path, extractor), READ_STATE_TIMEOUT_SECONDS
        )

    def close(self, session_id: str) -> None:
        self._submit(self._close(session_id), CLOSE_TIMEOUT_SECONDS)

    def close_if_owned(self, session_id: str) -> None:
        """拆除属于本 broker 的登录浏览器；非本 broker 的会话（如发布会话）安静 no-op，且不唤起 loop。"""
        with self._lock:
            running = (
                self._loop is not None and self._thread is not None and self._thread.is_alive()
            )
        if not running or session_id not in self._contexts:
            return
        try:
            self.close(session_id)
        except Exception:
            _logger.warning("Failed to close login browser %s", session_id, exc_info=True)

    def owns(self, session_id: str) -> bool:
        return session_id in self._contexts

    def active_count(self) -> int:
        return len(self._contexts)

    def shutdown(self) -> None:
        """停掉 loop 线程并拆掉所有在用浏览器（best-effort，worker 退出 / 测试清理用）。"""
        with self._lock:
            loop = self._loop
            thread = self._thread
            self._loop = None
            self._thread = None
        if loop is None:
            return

        async def _shutdown_all() -> None:
            for session_id in list(self._contexts.keys()):
                await self._close(session_id)

        try:
            asyncio.run_coroutine_threadsafe(_shutdown_all(), loop).result(
                timeout=CLOSE_TIMEOUT_SECONDS
            )
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)

    # ── 协程（都在 loop 线程上跑，故对 self._contexts 的读写天然无竞态）────────

    async def _launch(
        self, session_id: str, profile_dir: Path, options: dict[str, Any], display: str
    ) -> None:
        if session_id in self._contexts:
            return
        if len(self._contexts) >= self._cap:
            raise ClientError(
                f"并发登录浏览器已达上限（{self._cap}），请先完成或取消已有登录后再试"
            )
        pw, context, page = await _pw_open(profile_dir, options, display)
        self._contexts[session_id] = (pw, context, page)

    async def _load_page(self, session_id: str, home_url: str) -> None:
        entry = self._contexts.get(session_id)
        if entry is None:
            return
        _pw, context, page = entry
        pages = list(getattr(context, "pages", []) or [])
        if pages:
            page = pages[-1]
        if page is None:
            return
        await _pw_goto(page, home_url)

    async def _read(
        self,
        session_id: str,
        detect: Callable[[str, str, str], bool],
        state_path: Path,
        extractor: ExtractorAsync | None = None,
    ) -> LoginBrowserResult:
        entry = self._contexts.get(session_id)
        if entry is None:
            raise ClientError(f"Remote browser session not found: {session_id}")
        _pw, context, page = entry
        pages = list(getattr(context, "pages", []) or [])
        if pages:
            page = pages[-1]
        if page is None:
            raise ClientError("Remote browser session has no page")
        url, title, body = await _pw_read(context, page)
        await _pw_storage_state(context, state_path)
        logged_in = bool(detect(url, title, body))

        # 已登录且传了 extractor → 在 live async page 上抽 creator-ID（best-effort，绝不抛）
        extracted: str | None = None
        if logged_in and extractor is not None:
            try:
                extracted = await extractor(page=page)
            except Exception:
                _logger.warning("creator-id extractor failed during login read", exc_info=True)
                extracted = None

        return LoginBrowserResult(
            logged_in=logged_in,
            url=url,
            title=title,
            extracted_platform_user_id=extracted,
        )

    async def _close(self, session_id: str) -> None:
        entry = self._contexts.pop(session_id, None)
        if entry is None:
            return
        pw, context, _page = entry
        await _pw_close(pw, context)


login_broker = LoginBrowserBroker()
