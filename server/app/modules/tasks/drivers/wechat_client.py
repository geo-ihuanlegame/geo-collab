"""微信公众号服务端 API 客户端（纯函数）。

约束：不碰 ORM、不读环境变量；所有函数显式收 httpx.Client（测试注入 MockTransport）。
token 的 DB 缓存读写在 runner 侧（见 runner_api.py），本模块只管单次 HTTP 调用。
错误统一抛 WeChatApiError（errcode 非 0 / HTTP >= 400 / 网络错误）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

API_BASE = "https://api.weixin.qq.com"
TOKEN_REFRESH_SKEW_SECONDS = 300  # token 提前 5 分钟视为过期

# 常见 errcode 的中文运维提示
_ERRCODE_HINTS = {
    40164: "请把服务器出口公网 IP 加入公众平台「设置与开发 → 基本配置 → IP 白名单」",
    40001: "AppSecret 无效或已被重置，请在账号管理里更新凭据",
}


class WeChatApiError(Exception):
    """微信接口错误：errcode 非 0、HTTP 错误或网络不可达。"""

    def __init__(
        self,
        message: str,
        errcode: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.errcode = errcode
        self.payload = payload or {}


def _parse_response(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise WeChatApiError(f"微信接口返回非 JSON 响应: HTTP {response.status_code}") from exc
    if response.status_code >= 400:
        raise WeChatApiError(f"微信接口 HTTP 错误: {response.status_code}", payload=payload)
    errcode = payload.get("errcode")
    if errcode not in (None, 0):
        errmsg = payload.get("errmsg", "unknown error")
        hint = _ERRCODE_HINTS.get(errcode)
        message = f"微信接口错误 {errcode}: {errmsg}"
        if hint:
            message = f"{message}（{hint}）"
        raise WeChatApiError(message, errcode=errcode, payload=payload)
    return payload


def _request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise WeChatApiError(f"微信接口不可达: {exc}") from exc
    return _parse_response(response)


def fetch_access_token(app_id: str, app_secret: str, *, client: httpx.Client) -> tuple[str, int]:
    """换取 access_token，返回 (token, expires_in 秒)。"""
    payload = _request(
        client,
        "GET",
        f"{API_BASE}/cgi-bin/token",
        params={"appid": app_id, "secret": app_secret, "grant_type": "client_credential"},
    )
    token = payload.get("access_token")
    expires_in = payload.get("expires_in")
    if not token or not expires_in:
        raise WeChatApiError("token 响应缺少 access_token", payload=payload)
    return token, int(expires_in)


def upload_thumb(access_token: str, filename: str, data: bytes, *, client: httpx.Client) -> str:
    """上传封面缩略图（永久素材，JPG ≤64KB），返回 thumb_media_id。"""
    payload = _request(
        client,
        "POST",
        f"{API_BASE}/cgi-bin/material/add_material",
        params={"access_token": access_token, "type": "thumb"},
        files={"media": (filename, data, "image/jpeg")},
    )
    media_id = payload.get("media_id")
    if not media_id:
        raise WeChatApiError("封面上传未返回 media_id", payload=payload)
    return media_id


def upload_content_image(
    access_token: str, filename: str, data: bytes, *, client: httpx.Client
) -> str:
    """上传正文图（≤1MB JPG/PNG），返回微信图床 URL（外链图会被微信过滤，必须转传）。"""
    mime = "image/png" if filename.lower().endswith(".png") else "image/jpeg"
    payload = _request(
        client,
        "POST",
        f"{API_BASE}/cgi-bin/media/uploadimg",
        params={"access_token": access_token},
        files={"media": (filename, data, mime)},
    )
    url = payload.get("url")
    if not url:
        raise WeChatApiError("正文图上传未返回 url", payload=payload)
    return url


def build_draft_article(*, title: str, content_html: str, thumb_media_id: str) -> dict[str, Any]:
    """构建 draft/add 的单篇 article 结构。

    digest/author 留空（微信自动取正文前 54 字 / 不显示作者）、评论默认关——
    产品交互稿无配置入口，全自动推导。
    """
    return {
        "article_type": "news",
        "title": title,
        "author": "",
        "digest": "",
        "content": content_html,
        "content_source_url": "",
        "thumb_media_id": thumb_media_id,
        "need_open_comment": 0,
        "only_fans_can_comment": 0,
    }


def add_draft(access_token: str, article: dict[str, Any], *, client: httpx.Client) -> str:
    """新增单图文草稿，返回草稿 media_id。"""
    body = json.dumps({"articles": [article]}, ensure_ascii=False).encode("utf-8")
    payload = _request(
        client,
        "POST",
        f"{API_BASE}/cgi-bin/draft/add",
        params={"access_token": access_token},
        headers={"Content-Type": "application/json; charset=utf-8"},
        content=body,
    )
    media_id = payload.get("media_id")
    if not media_id:
        raise WeChatApiError("草稿创建未返回 media_id", payload=payload)
    return media_id


def make_default_client() -> httpx.Client:
    """生产用默认 client（上传超时放宽到 60s）。调用方负责 close。"""
    return httpx.Client(timeout=httpx.Timeout(20.0, read=60.0, write=60.0))
