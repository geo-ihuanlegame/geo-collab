"""微信公众号 API 驱动：草稿箱单图文发布（mode='api'，无浏览器）。

链路：封面（无则回落正文首图）压 JPG≤64KB 传 thumb → 正文图逐张压 ≤1MB 转传换
微信 URL → body_segments 重组 HTML → draft/add。终点即草稿箱（spec：不调 freepublish）。
驱动纯数据进出：凭据/token 由 runner_api 解析后经 payload 注入，不碰 ORM。
"""

from __future__ import annotations

import html as html_lib

import httpx

from server.app.modules.articles.parser import BodySegment
from server.app.modules.tasks.drivers import register
from server.app.modules.tasks.drivers.base import (
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
from server.app.modules.tasks.drivers.wechat_images import (
    compress_content_image,
    compress_cover_to_jpeg,
)


def segments_to_html(segments: list[BodySegment], image_urls: dict[int, str]) -> str:
    """body_segments → 微信草稿 HTML。image_urls 按 segment 下标映射微信图床 URL。"""
    parts: list[str] = []
    for index, seg in enumerate(segments):
        if seg.kind == "image":
            url = image_urls.get(index)
            if url:
                parts.append(f'<p><img src="{url}" style="max-width:100%;"></p>')
            continue
        text = html_lib.escape(seg.text).replace("\n", "<br>")
        if not text.strip():
            continue
        if seg.heading_level == 1:
            parts.append(f"<h1>{text}</h1>")
        elif seg.heading_level == 2:
            parts.append(f"<h2>{text}</h2>")
        elif seg.bold:
            parts.append(f"<p><strong>{text}</strong></p>")
        else:
            parts.append(f"<p>{text}</p>")
    return "".join(parts)


class WeChatMpDriver:
    code = "wechat_mp"
    name = "微信公众号"
    home_url = "https://mp.weixin.qq.com"
    publish_url = "https://mp.weixin.qq.com"
    mode = "api"  # build_publish_runner_for_record 据此走 runner_api 路径

    def detect_logged_in(self, *, url: str, title: str, body: str) -> bool:
        return False  # API 平台不走浏览器登录检测

    def publish(self, *, page, context, payload, stop_before_publish):  # pragma: no cover
        raise PublishError("微信公众号为 API 接入，不支持浏览器发布路径")

    def publish_api(
        self, *, payload: ApiPublishPayload, client: httpx.Client | None = None
    ) -> PublishResult:
        owns_client = client is None
        if client is None:
            client = make_default_client()
        try:
            return self._publish_api(payload=payload, client=client)
        except WeChatApiError as exc:
            raise PublishError(str(exc)) from exc
        finally:
            if owns_client:
                client.close()

    def _publish_api(self, *, payload: ApiPublishPayload, client: httpx.Client) -> PublishResult:
        token = payload.access_token

        cover_path = payload.cover_path
        if cover_path is None:
            cover_path = next(
                (s.image_path for s in payload.body_segments if s.kind == "image" and s.image_path),
                None,
            )
        if cover_path is None:
            raise PublishError("公众号草稿需要封面图（或正文至少一张图）")
        thumb_media_id = upload_thumb(
            token, "cover.jpg", compress_cover_to_jpeg(cover_path.read_bytes()), client=client
        )

        image_urls: dict[int, str] = {}
        for index, seg in enumerate(payload.body_segments):
            if seg.kind != "image" or seg.image_path is None:
                continue
            data, filename = compress_content_image(
                seg.image_path.read_bytes(), seg.image_path.name
            )
            image_urls[index] = upload_content_image(token, filename, data, client=client)

        content_html = segments_to_html(payload.body_segments, image_urls)
        if not content_html:
            raise PublishError("正文为空，无法创建公众号草稿")
        article = build_draft_article(
            title=payload.title, content_html=content_html, thumb_media_id=thumb_media_id
        )
        media_id = add_draft(token, article, client=client)
        return PublishResult(
            url=None,
            title=payload.title,
            message=f"草稿已写入公众号草稿箱 media_id={media_id}",
        )


register(WeChatMpDriver())
