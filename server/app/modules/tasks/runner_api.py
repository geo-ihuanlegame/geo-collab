"""API 型平台发布入口：不起浏览器，token 的 DB 读写在这里完成（驱动不碰 ORM）。

与 runner.run_publish 的关系：build_publish_runner_for_record 按驱动 mode 分叉，
API 驱动进本模块。资产解析复用 runner 的 stock image 拉取与临时文件清理。
"""

from __future__ import annotations

import time
from pathlib import Path

from server.app.core.paths import get_data_dir
from server.app.modules.accounts.models import Account
from server.app.modules.accounts.secret_files import read_state
from server.app.modules.articles.models import Article
from server.app.modules.articles.parser import (
    BodySegment,
    extract_body_image_nodes,
    extract_body_stock_image_nodes,
    loads_content_json,
    parse_body_segments,
)
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
                image_path = _resolve_stock_image_path(seg.stock_image_id, missing_ok=True)
                if image_path is None:
                    continue  # 图库图已删除：跳过该图，照常发布（#36）
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


def _build_cookie_payload(
    article: Article, account: Account, platform_code: str
) -> ApiPublishPayload:
    """cookie-session 驱动（TapTap）payload：读解密 storage_state + 论坛配置 + content_json 图片→本地路径。

    驱动不碰 ORM：这里把登录态（cookie 罐）/ 论坛配置 / 图片本地路径全解析成纯数据注入。
    图片 key 用 asset_id 或 ``stock:<id>``，与转换器 image_node_key 一致（按 key 查、不依赖顺序）。
    """
    from server.app.modules.tasks.runner import (
        _cleanup_temp_files,
        _resolve_stock_image_path,
    )

    if not account.state_path:
        raise PublishError("TapTap 账号缺登录态（storage_state），请先在媒体矩阵登录")
    abs_state = get_data_dir() / account.state_path
    if not abs_state.exists():
        raise PublishError(f"TapTap 登录态文件不存在: {account.state_path}，请重新登录")
    state = read_state(abs_state)
    forum = dict(account.api_credentials or {})
    # x_ua 未显式配置时，从 platform_user_id(VID) 合成（VID 由登录 / cookie 体检回填）。
    if not forum.get("x_ua") and account.platform_user_id:
        from server.app.modules.tasks.drivers.taptap_client import build_x_ua

        forum["x_ua"] = build_x_ua(account.platform_user_id)

    content_json = loads_content_json(article.content_json)
    if not content_json:
        body = (article.plain_text or "").strip()
        content_json = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": ([{"type": "text", "text": body}] if body else []),
                }
            ],
        }

    image_paths: dict[str, Path] = {}
    temp_files: list[Path] = []
    try:
        for asset_id, _node_id in extract_body_image_nodes(content_json):
            if asset_id in image_paths:
                continue
            asset_link = next(
                (
                    link
                    for link in article.body_assets
                    if link.asset_id == asset_id and link.asset is not None
                ),
                None,
            )
            if asset_link is None:
                raise PublishError(f"正文图片资源不存在或未加载: {asset_id}")
            image_paths[asset_id] = resolve_asset_path(asset_link.asset)
        for stock_id in extract_body_stock_image_nodes(content_json):
            key = f"stock:{stock_id}"
            if key in image_paths:
                continue
            image_path = _resolve_stock_image_path(stock_id, missing_ok=True)
            if image_path is None:
                continue  # 图库图已删除：跳过该图，照常发布（#36）
            temp_files.append(image_path)
            image_paths[key] = image_path
        return ApiPublishPayload(
            title=article.title,
            body_segments=[],
            cover_path=None,
            display_name=account.display_name,
            platform_code=platform_code,
            state=state,
            forum=forum,
            content_json=content_json,
            image_paths=image_paths,
            temp_files=tuple(temp_files),
        )
    except Exception:
        _cleanup_temp_files(temp_files)
        raise


def run_publish_api(
    *,
    article: Article,
    account: Account,
    driver,
    platform_code: str,
    commit_guard=None,
    retry_policy=None,
) -> PublishResult:
    """API 平台发布：按 driver.auth 解析鉴权 → payload 构建 → driver.publish_api。

    platform_code 由 build_publish_runner_for_record 传入（=record.platform.code），避免在发布线程里
    懒加载已 detached 的 account.platform（见 #90）。
    auth='token'（公众号）拉 access_token；auth='cookie'（TapTap）读 storage_state + 论坛配置。
    stop_before_publish 对终点是 no-op（草稿箱 / 公开发布即终点），故无此参数。
    commit_guard/retry_policy 可选：默认 NOOP_COMMIT_GUARD/None，跨提交点弹性（#133）。
    """
    from server.app.modules.tasks.drivers.base import NOOP_COMMIT_GUARD
    from server.app.modules.tasks.runner import _cleanup_temp_files

    if commit_guard is None:
        commit_guard = NOOP_COMMIT_GUARD

    if not article.title or not article.title.strip():
        raise PublishError("标题不能为空")

    auth = getattr(driver, "auth", "token")
    if auth == "cookie":
        payload = _build_cookie_payload(article, account, platform_code)
    else:
        with publish_step("resolve api access token"):
            access_token = _resolve_access_token(account.id)
        payload = _build_api_payload(article, account, access_token, platform_code)
    try:
        with publish_step("api driver publish flow"):
            return driver.publish_api(
                payload=payload, commit_guard=commit_guard, retry_policy=retry_policy
            )
    finally:
        _cleanup_temp_files(payload.temp_files)
