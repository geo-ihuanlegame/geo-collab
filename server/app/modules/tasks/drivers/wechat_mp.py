"""微信公众号 API 驱动：草稿箱单图文发布（mode='api'，无浏览器）。

链路：封面（无则回落正文首图）压 JPG≤64KB 传 thumb → 正文图逐张压 ≤1MB 转传换
微信 URL → tiptap_to_wechat_html 生成保真 HTML → draft/add。终点即草稿箱（spec：不调 freepublish）。
驱动纯数据进出：凭据/token 由 runner_api 解析后经 payload 注入，不碰 ORM。
"""

from __future__ import annotations

import httpx

from server.app.modules.tasks.drivers import register
from server.app.modules.tasks.drivers.base import (
    NOOP_COMMIT_GUARD,
    ApiPublishPayload,
    PublishError,
    PublishResult,
)
from server.app.modules.tasks.drivers.wechat_client import (
    WeChatApiError,
    add_draft,
    build_draft_article,
    make_default_client,
    upload_content_image,
    upload_thumb,
)
from server.app.modules.tasks.drivers.wechat_html import tiptap_to_wechat_html
from server.app.modules.tasks.drivers.wechat_images import (
    compress_content_image,
    compress_cover_to_jpeg,
)
from server.app.shared.resilience import RetryPolicy, retry_call


def _wechat_is_transient(exc: BaseException) -> bool:
    """WeChatApiError(errcode=None)=网络不可达(见 wechat_client._request)→可重试；
    errcode 非空=业务错误→永久不重试。"""
    if isinstance(exc, WeChatApiError):
        return exc.errcode is None
    return False


class WeChatMpDriver:
    code = "wechat_mp"
    name = "微信公众号"
    home_url = "https://mp.weixin.qq.com"
    publish_url = "https://mp.weixin.qq.com"
    mode = "api"  # build_publish_runner_for_record 据此走 runner_api 路径

    def detect_logged_in(self, *, url: str, title: str, body: str) -> bool:
        return False  # API 平台不走浏览器登录检测

    def extract_platform_user_id_sync(self, *, page) -> str | None:  # pragma: no cover
        return None  # API 平台不抽取浏览器侧 creator-ID（platform_user_id 即 AppID）

    async def extract_platform_user_id_async(self, *, page) -> str | None:  # pragma: no cover
        return None  # API 平台不抽取浏览器侧 creator-ID（platform_user_id 即 AppID）

    def publish(
        self, *, page, context, payload, stop_before_publish, commit_guard=None, retry_policy=None
    ):  # pragma: no cover
        raise PublishError("微信公众号为 API 接入，不支持浏览器发布路径")

    def publish_api(
        self,
        *,
        payload: ApiPublishPayload,
        client: httpx.Client | None = None,
        commit_guard=None,
        retry_policy=None,
    ) -> PublishResult:
        if commit_guard is None:
            commit_guard = NOOP_COMMIT_GUARD
        policy = retry_policy or RetryPolicy()
        owns_client = client is None
        if client is None:
            client = make_default_client()
        try:
            return self._publish_api(
                payload=payload, client=client, commit_guard=commit_guard, policy=policy
            )
        except WeChatApiError as exc:
            raise PublishError(str(exc)) from exc
        finally:
            if owns_client:
                client.close()

    def _publish_api(
        self, *, payload: ApiPublishPayload, client: httpx.Client, commit_guard, policy
    ) -> PublishResult:
        token = payload.access_token
        image_paths = payload.image_paths or {}

        cover_path = payload.cover_path
        if cover_path is None:
            cover_path = next(iter(image_paths.values()), None)
        if cover_path is None:
            raise PublishError("公众号草稿需要封面图（或正文至少一张图）")

        def _do_thumb() -> str:
            return upload_thumb(
                token, "cover.jpg", compress_cover_to_jpeg(cover_path.read_bytes()), client=client
            )

        thumb_media_id = retry_call(_do_thumb, policy=policy, is_transient=_wechat_is_transient)

        image_urls: dict[str, str] = {}
        for key, path in image_paths.items():
            data, filename = compress_content_image(path.read_bytes(), path.name)

            def _do_content_image(_data: bytes = data, _filename: str = filename) -> str:
                return upload_content_image(token, _filename, _data, client=client)

            image_urls[key] = retry_call(
                _do_content_image, policy=policy, is_transient=_wechat_is_transient
            )

        content_html = tiptap_to_wechat_html(payload.content_json or {}, image_urls)
        if not content_html:
            raise PublishError("正文为空，无法创建公众号草稿")
        article = build_draft_article(
            title=payload.title, content_html=content_html, thumb_media_id=thumb_media_id
        )
        # 提交边界：add_draft 非幂等，不进 retry_call，只进 commit_guard
        with commit_guard.committing():
            media_id = add_draft(token, article, client=client)
        return PublishResult(
            url=None,
            title=payload.title,
            message=f"草稿已写入公众号草稿箱 media_id={media_id}",
        )


register(WeChatMpDriver())
