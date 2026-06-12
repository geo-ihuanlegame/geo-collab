"""API 型平台发布入口：不起浏览器，token 的 DB 读写在这里完成（驱动不碰 ORM）。

与 runner.run_publish 的关系：build_publish_runner_for_record 按驱动 mode 分叉，
API 驱动进本模块。资产解析复用 runner 的 stock image 拉取与临时文件清理。
"""

from __future__ import annotations

import time
from pathlib import Path

from server.app.modules.accounts.models import Account
from server.app.modules.articles.models import Article
from server.app.modules.articles.parser import BodySegment, parse_body_segments
from server.app.modules.articles.store import resolve_asset_path
from server.app.modules.tasks.drivers.base import ApiPublishPayload, PublishError, PublishResult
from server.app.modules.tasks.drivers.wechat_client import (
    fetch_access_token,
    make_default_client,
)
from server.app.shared.diagnostics import publish_step


def _resolve_access_token(account_id: int) -> str:
    """读 DB token 缓存，过期则刷新并写回。自开 session（发布线程内，不复用外部 session）。"""
    from server.app.db.session import SessionLocal
    from server.app.modules.accounts.service import get_cached_wechat_token

    db = SessionLocal()
    try:
        account = db.get(Account, account_id)
        if account is None:
            raise PublishError(f"账号不存在: {account_id}")
        token = get_cached_wechat_token(account)
        if token:
            return token
        creds = account.api_credentials or {}
        if not creds.get("app_id") or not creds.get("app_secret"):
            raise PublishError("账号未配置 AppID/AppSecret，请先在媒体矩阵完成授权")
        client = make_default_client()
        try:
            token, expires_in = fetch_access_token(
                creds["app_id"], creds["app_secret"], client=client
            )
        finally:
            client.close()
        account.api_token_cache = {
            "access_token": token,
            "expires_at": int(time.time()) + expires_in,
        }
        db.commit()
        return token
    finally:
        db.close()


def _build_api_payload(
    article: Article, account: Account, access_token: str, platform_code: str
) -> ApiPublishPayload:
    """解析正文段与资产路径（含图片库临时文件）。封面可空——驱动内回落正文首图。

    platform_code 由调用方（build_publish_runner_for_record，权威值=record.platform.code）显式传入，
    不读 account.platform——发布线程里 account 已 detached，懒加载该关系会抛 DetachedInstanceError（见 #90）。
    """
    from server.app.modules.tasks.runner import (
        _cleanup_temp_files,
        _resolve_stock_image_path,
    )

    cover_path: Path | None = None
    if article.cover_asset is not None:
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
                        kind="image", stock_image_id=seg.stock_image_id, image_path=image_path
                    )
                )
            else:
                resolved.append(seg)
        return ApiPublishPayload(
            title=article.title,
            body_segments=resolved,
            cover_path=cover_path,
            display_name=account.display_name,
            platform_code=platform_code,
            access_token=access_token,
            temp_files=tuple(temp_files),
        )
    except Exception:
        _cleanup_temp_files(temp_files)
        raise


def run_publish_api(
    *, article: Article, account: Account, driver, platform_code: str
) -> PublishResult:
    """API 平台发布：token 解析 → payload 构建 → driver.publish_api。

    platform_code 由 build_publish_runner_for_record 传入（=record.platform.code），避免在发布线程里
    懒加载已 detached 的 account.platform（见 #90）。
    stop_before_publish 对草稿箱终点是 no-op（草稿箱本身就是「停在发布前」），故无此参数。
    """
    from server.app.modules.tasks.runner import _cleanup_temp_files

    if not article.title or not article.title.strip():
        raise PublishError("标题不能为空")

    with publish_step("resolve api access token"):
        access_token = _resolve_access_token(account.id)
    payload = _build_api_payload(article, account, access_token, platform_code)
    try:
        with publish_step("api driver publish flow"):
            return driver.publish_api(payload=payload)
    finally:
        _cleanup_temp_files(payload.temp_files)
