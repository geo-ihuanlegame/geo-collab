"""
发布运行器：单条记录的浏览器自动化入口。

run_publish 负责在 Playwright 持久化 context 里跑驱动的 publish 流程——按账号 platform 选驱动、
复用或新建远程浏览器会话（Xvfb + noVNC 那套）、把 ORM 对象解析成 PublishPayload（含本地资源路径）
后交给驱动。驱动只拿 page/context/payload，不碰 DB。

注意：Playwright sync API 用 thread-local greenlet——context 若是在已退出的别的线程里建的，
必须先销毁会话再重建（见 run_publish 里的 context_thread_id 检查），否则 new_page() 抛 greenlet.error。
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from server.app.core.paths import get_data_dir
from server.app.modules.accounts.browser import (
    associate_record_with_session,
    attach_browser_handles,
    get_or_create_account_session,
    keep_session_alive,
    stop_remote_browser_session,
)
from server.app.modules.accounts.models import Account
from server.app.modules.accounts.service import (
    account_key_from_state_path,
    clear_profile_locks,
    launch_options,
    profile_dir_from_state_path,
    profile_key_from_state_path,
)
from server.app.modules.articles.models import Article
from server.app.modules.articles.parser import BodySegment, parse_body_segments
from server.app.modules.articles.store import resolve_asset_path
from server.app.modules.tasks.drivers import resolve_driver
from server.app.modules.tasks.drivers.base import (
    PublishError,
    PublishPayload,
    PublishResult,
    UserInputRequired,
)
from server.app.shared.diagnostics import publish_step, record_publish_diagnostic


def _short_url(url: str, limit: int = 180) -> str:
    return url if len(url) <= limit else f"{url[:limit]}..."


def _attach_page_network_diagnostics(page: Any) -> None:
    counters = {"failed": 0, "bad_response": 0}

    def on_request_failed(request: Any) -> None:
        if counters["failed"] >= 20:
            return
        counters["failed"] += 1
        try:
            failure = getattr(request, "failure", None)
            if callable(failure):
                failure = failure()
            error_text = failure or "unknown"
            record_publish_diagnostic(
                f"network request failed: {request.method} {_short_url(request.url)}; error={error_text}",
                level="warn",
            )
        except Exception:
            record_publish_diagnostic(
                "network request failed: unable to read request details", level="warn"
            )

    def on_response(response: Any) -> None:
        try:
            status = int(response.status)
        except Exception:
            return
        if status < 400 or counters["bad_response"] >= 20:
            return
        counters["bad_response"] += 1
        try:
            record_publish_diagnostic(
                f"network response status={status}: {response.request.method} {_short_url(response.url)}",
                level="warn",
            )
        except Exception:
            record_publish_diagnostic(
                f"network response status={status}: unable to read response details", level="warn"
            )

    page.on("requestfailed", on_request_failed)
    page.on("response", on_response)


def _cleanup_temp_files(paths: list[Path] | tuple[Path, ...]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            record_publish_diagnostic(f"stock image temp cleanup failed: {path}", level="warn")


def _stock_image_suffix(filename: str, minio_key: str) -> str:
    suffix = Path(filename).suffix or Path(minio_key).suffix
    return suffix if suffix else ".jpg"


def _resolve_stock_image_path(stock_image_id: int) -> Path:
    """把图片库（MinIO）里的图拉到本地临时文件，返回路径（调用方负责后续清理 temp_files）。

    自开独立 SessionLocal 查 StockImage/StockCategory（本函数可能在发布线程里调，不复用主 session）。
    """
    from server.app.db.session import SessionLocal
    from server.app.modules.image_library import store as minio_store
    from server.app.modules.image_library.models import StockCategory, StockImage

    db = SessionLocal()
    try:
        img = db.get(StockImage, stock_image_id)
        if img is None:
            raise PublishError(f"图片库图片不存在: {stock_image_id}")
        cat = db.get(StockCategory, img.category_id)
        if cat is None:
            raise PublishError(f"图片库栏目不存在: {img.category_id}")
        data = minio_store.get_object_bytes(cat.bucket_name, img.minio_key)
        suffix = _stock_image_suffix(img.filename, img.minio_key)
    finally:
        db.close()

    tmp = tempfile.NamedTemporaryFile(
        prefix=f"geo_stock_{stock_image_id}_", suffix=suffix, delete=False
    )
    tmp_path = Path(tmp.name)
    try:
        tmp.write(data)
        tmp.close()
    except Exception:
        tmp.close()
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


def _build_payload(
    article: Article,
    account: Account,
    account_key: str,
    platform_code: str,
    state_path: Path,
) -> PublishPayload:
    """Resolve all asset paths from ORM objects and build PublishPayload.

    Must be called before entering the Playwright session so ORM relationships
    are still accessible and drivers never need DB access during automation.
    """
    if article.cover_asset is None:
        raise PublishError("封面图片是必填项")
    cover_path = resolve_asset_path(article.cover_asset)

    raw_segments = parse_body_segments(article)
    resolved: list[BodySegment] = []
    for seg in raw_segments:
        if seg.kind == "image" and seg.image_asset_id:
            asset_link = next(
                (
                    link
                    for link in article.body_assets
                    if link.asset_id == seg.image_asset_id and link.asset is not None
                ),
                None,
            )
            if asset_link is None:
                raise PublishError(f"正文图片资源不存在或未加载: {seg.image_asset_id}")
            resolved.append(
                BodySegment(
                    kind="image",
                    image_asset_id=seg.image_asset_id,
                    image_path=resolve_asset_path(asset_link.asset),
                )
            )
        else:
            resolved.append(seg)

    return PublishPayload(
        title=article.title,
        cover_asset_path=cover_path,
        body_segments=resolved,
        account_key=account_key,
        state_path=state_path,
        display_name=account.display_name,
        platform_code=platform_code,
    )


def _build_payload_with_stock_images(
    article: Article,
    account: Account,
    account_key: str,
    platform_code: str,
    state_path: Path,
) -> PublishPayload:
    """构建 PublishPayload，正文图既支持本地 body_asset 也支持图片库 stock_image（后者拉到临时文件）。

    把生成的临时文件挂到 payload.temp_files，发布结束后由调用方清理；中途出错就地清空临时文件再抛。
    """
    if article.cover_asset is None:
        raise PublishError("封面图片是必填项")
    cover_path = resolve_asset_path(article.cover_asset)

    raw_segments = parse_body_segments(article)
    resolved: list[BodySegment] = []
    temp_files: list[Path] = []
    try:
        for seg in raw_segments:
            if seg.kind == "image" and seg.image_asset_id:
                asset_link = next(
                    (
                        link
                        for link in article.body_assets
                        if link.asset_id == seg.image_asset_id and link.asset is not None
                    ),
                    None,
                )
                if asset_link is None:
                    raise PublishError(f"正文图片资源不存在或未加载: {seg.image_asset_id}")
                resolved.append(
                    BodySegment(
                        kind="image",
                        image_asset_id=seg.image_asset_id,
                        image_path=resolve_asset_path(asset_link.asset),
                    )
                )
            elif seg.kind == "image" and seg.stock_image_id is not None:
                image_path = _resolve_stock_image_path(seg.stock_image_id)
                temp_files.append(image_path)
                resolved.append(
                    BodySegment(
                        kind="image",
                        stock_image_id=seg.stock_image_id,
                        image_path=image_path,
                    )
                )
            else:
                resolved.append(seg)

        return PublishPayload(
            title=article.title,
            cover_asset_path=cover_path,
            body_segments=resolved,
            account_key=account_key,
            state_path=state_path,
            display_name=account.display_name,
            platform_code=platform_code,
            temp_files=tuple(temp_files),
        )
    except Exception:
        _cleanup_temp_files(temp_files)
        raise


# 实际生效的 payload 构建器：覆盖上面的 _build_payload，启用图片库（stock image）正文图支持
_build_payload = _build_payload_with_stock_images


def run_publish(
    *,
    record_id: int | None = None,
    article: Article,
    account: Account,
    channel: str = "chromium",
    executable_path: str | None = None,
    stop_before_publish: bool = False,
) -> PublishResult:
    """Generic publish entry point. Looks up driver by account platform, reuses or starts remote session, runs driver.publish."""
    if not article.title or not article.title.strip():
        raise PublishError("标题不能为空")
    if article.cover_asset is None:
        raise PublishError("封面图片是必填项")

    platform_code, account_key = account_key_from_state_path(account.state_path)
    profile_key = profile_key_from_state_path(account.state_path)
    profile_dir = profile_dir_from_state_path(account.state_path)
    state_path = (get_data_dir() / account.state_path).resolve()
    if not state_path.exists():
        raise PublishError(f"账号授权状态文件不存在: {account.state_path}")

    driver = resolve_driver(platform_code)

    # Resolve all asset paths before entering the browser session — ORM objects
    # may become detached once we hand control to Playwright threads.
    payload = _build_payload(article, account, account_key, platform_code, state_path)

    with publish_step("remote browser session"):
        session = get_or_create_account_session(platform_code, account_key, profile_key=profile_key)
        # Associate immediately so the timeout handler can stop this session.
        if record_id is not None:
            associate_record_with_session(record_id, session.id)

    # Playwright sync API uses greenlets that are thread-local. If the context
    # was created in a different thread (which has since exited), we must tear
    # down that session and start fresh; attempting context.new_page() from
    # the wrong thread raises greenlet.error.
    current_thread_id = threading.get_ident()
    if session.browser_context is not None and session.context_thread_id != current_thread_id:
        # stop_remote_browser_session kills the Chromium process via OS signals
        # and wraps all Playwright API calls in try/except, so it is safe to
        # call from any thread even when the original greenlet thread has exited.
        stop_remote_browser_session(session.id)
        with publish_step("remote browser session (re-acquire after thread switch)"):
            session = get_or_create_account_session(
                platform_code, account_key, profile_key=profile_key
            )
            if record_id is not None:
                associate_record_with_session(record_id, session.id)

    if session.browser_context is None:
        pw = None
        try:
            with publish_step("start Playwright"):
                pw = sync_playwright().start()
            with publish_step("launch Chromium"):
                clear_profile_locks(profile_dir)
                options = launch_options(channel, executable_path)
                options["env"] = {**os.environ, "DISPLAY": session.display}
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    **options,
                )
                context.set_default_navigation_timeout(30000)
                grant_permissions = getattr(context, "grant_permissions", None)
                if callable(grant_permissions):
                    grant_permissions(["clipboard-read", "clipboard-write"])
                attach_browser_handles(
                    session.id, pw, context, None, context_thread_id=current_thread_id
                )
        except Exception:
            if pw is not None:
                try:
                    pw.stop()
                except Exception:
                    pass
            stop_remote_browser_session(session.id)
            raise
    else:
        context = session.browser_context

    page = None
    # _keep_browser=True 时保活会话不拆（停在预览待手动确认 / 待人工接管），其余路径 finally 必拆
    _keep_browser = False
    try:
        page = context.new_page()
        _attach_page_network_diagnostics(page)
        with publish_step("driver publish flow", page=page):
            result = driver.publish(
                page=page,
                context=context,
                payload=payload,
                stop_before_publish=stop_before_publish,
            )
            if stop_before_publish:
                _keep_browser = True
                keep_session_alive(session.id)
            return result
    except UserInputRequired as exc:
        _keep_browser = True
        keep_session_alive(session.id)
        exc.session_id = session.id
        exc.novnc_url = session.novnc_url
        raise
    except Exception:
        # Destroy the session so the broken context is not reused on the
        # next publish attempt for this account.
        stop_remote_browser_session(session.id)
        raise
    finally:
        if page is not None and not _keep_browser:
            try:
                page.close()
            except Exception:
                pass
        if not _keep_browser:
            stop_remote_browser_session(session.id)
        _cleanup_temp_files(payload.temp_files)
