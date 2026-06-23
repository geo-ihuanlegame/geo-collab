"""TapTap webapiv2 纯 HTTP 客户端（无浏览器）。

照 spike `replay_httpx.py` 实测构造：
  auth = cookie 罐 + ``X-XSRF-TOKEN`` 头（取自 XSRF-TOKEN cookie、URL 解码）
       + ``X-UA`` 查询参数 + 浏览器式 UA/Origin/Referer/sec-* 头。
  body = ``application/x-www-form-urlencoded``；forum_bindings / contents / image_infos
       为 JSON 字符串字段。
发帖五步里驱动只需 create-topic → publish-topic（update-topic 是编辑器 autosave，
实测可省，spike create→publish 已验证）；图片走 image-upload-token → 七牛上传。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import unquote

import httpx

WEBAPI = "https://www.taptap.cn/webapiv2"
QINIU_UPLOAD = "https://upload.qiniup.com/"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)


class TapTapApiError(Exception):
    """TapTap 接口失败（非鉴权类）。"""

    def __init__(self, message: str, *, status: int | None = None, code: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        # 有 HTTP 响应（status 非空）即视为「服务端已应答、未受理」→ 暴露 errcode 供 CommitGuard
        # 判为干净失败（post 必未发出），不被误包成 CommitUncertainError 而挡掉合理重试（#133）。
        self.errcode = code if code is not None else status


class TapTapAuthError(TapTapApiError):
    """cookie 失效 / 未登录（HTTP 401/403 或鉴权类业务错误）。驱动据此提示重登。"""


def extract_cookies_and_xsrf(state: dict[str, Any]) -> tuple[dict[str, str], str | None]:
    """从 Playwright storage_state 取 taptap 域 cookie 罐 + 解码后的 XSRF token。"""
    jar: dict[str, str] = {}
    xsrf: str | None = None
    for cookie in state.get("cookies", []) or []:
        if "taptap" not in (cookie.get("domain") or ""):
            continue
        name, value = cookie.get("name"), cookie.get("value")
        if not name:
            continue
        jar[name] = value
        if name == "XSRF-TOKEN":
            xsrf = unquote(value or "")
    return jar, xsrf


def build_headers(xsrf: str, *, app_id: int | str, group_id: int | str) -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "Origin": "https://www.taptap.cn",
        "Referer": f"https://www.taptap.cn/creator/edit?type=topic&app_id={app_id}&group_id={group_id}",
        "X-XSRF-TOKEN": xsrf,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        "sec-ch-ua": '"Chromium";v="143", "Not(A:Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }


def make_client(
    state: dict[str, Any],
    *,
    app_id: int | str,
    group_id: int | str,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> httpx.Client:
    """构造带 cookie 罐 + 鉴权头的 httpx.Client。transport 仅供测试注入 MockTransport。"""
    cookies, xsrf = extract_cookies_and_xsrf(state)
    if not xsrf:
        raise TapTapAuthError("storage_state 缺 XSRF-TOKEN cookie，需重新登录 TapTap")
    return httpx.Client(
        cookies=cookies,
        headers=build_headers(xsrf, app_id=app_id, group_id=group_id),
        timeout=timeout,
        transport=transport,
    )


def build_forum_bindings(group_id: int | str) -> list[dict[str, Any]]:
    return [
        {
            "group_id": int(group_id),
            "group_label_id": 0,
            "honor_title": "",
            "honor_obj_id": "",
            "honor_obj_type": "",
            "is_official": False,
            "is_on_hosts_behalf": False,
        }
    ]


def _topic_fields(
    *,
    title: str,
    contents: list[dict[str, Any]],
    forum_bindings: list[dict[str, Any]],
    image_infos: list[dict[str, Any]] | None,
) -> dict[str, str]:
    return {
        "type": "0",
        "forum_bindings": json.dumps(forum_bindings, ensure_ascii=False),
        "hashtag_ids": "",
        "title": title,
        "publish_time": "",
        "aigc_type": "0",
        "contents": json.dumps(contents, ensure_ascii=False),
        "image_infos": json.dumps(image_infos or [], ensure_ascii=False),
    }


def _error_message(body: dict[str, Any]) -> str:
    for path in (
        ("data", "msg"),
        ("data", "message"),
        ("msg",),
        ("message",),
        ("error", "msg"),
        ("error", "message"),
    ):
        cur: Any = body
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                cur = None
                break
        if isinstance(cur, str) and cur:
            return cur
    return json.dumps(body, ensure_ascii=False)[:300]


def _post_form(
    client: httpx.Client, path: str, *, x_ua: str, fields: dict[str, str]
) -> dict[str, Any]:
    resp = client.post(f"{WEBAPI}/{path}", params={"X-UA": x_ua}, data=fields)
    if resp.status_code in (401, 403):
        raise TapTapAuthError(
            f"TapTap 返回 HTTP {resp.status_code}，cookie 失效或无权限，请重新登录",
            status=resp.status_code,
        )
    try:
        body = resp.json()
    except Exception as exc:  # 非 JSON（多为风控拦截页）
        raise TapTapApiError(
            f"TapTap {path} 响应非 JSON (HTTP {resp.status_code}): {resp.text[:300]}",
            status=resp.status_code,
        ) from exc
    if not body.get("success"):
        raise TapTapApiError(f"TapTap {path} 失败: {_error_message(body)}", status=resp.status_code)
    data = body.get("data")
    return data if isinstance(data, dict) else {}


def get_image_upload_token(client: httpx.Client, *, x_ua: str) -> str:
    """取七牛上传 token。"""
    data = _post_form(
        client,
        "send-file/v1/image-upload-token",
        x_ua=x_ua,
        fields={"sdk": "qiniu:3.3.3", "type": "moment"},
    )
    token = data.get("token")
    if not token:
        raise TapTapApiError(
            f"image-upload-token 未返回 token: {json.dumps(data, ensure_ascii=False)[:200]}"
        )
    return token


def upload_image_to_qiniu(
    token: str,
    file_bytes: bytes,
    filename: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """七牛标准表单上传，返回 {"url": ..., "info": {...}}（saveKey 由 token returnBody 模板服务端生成）。

    走独立请求（不带 taptap cookie/头）；client 仅供测试注入。
    """
    owns = client is None
    cli = client or httpx.Client(timeout=60.0)
    try:
        resp = cli.post(
            QINIU_UPLOAD,
            data={"token": token},
            files={"file": (filename, file_bytes)},
        )
        try:
            body = resp.json()
        except Exception as exc:
            raise TapTapApiError(
                f"七牛上传响应非 JSON (HTTP {resp.status_code}): {resp.text[:300]}",
                status=resp.status_code,
            ) from exc
        url = body.get("url")
        if not url:
            raise TapTapApiError(
                f"七牛上传未返回 url: {json.dumps(body, ensure_ascii=False)[:200]}"
            )
        return {"url": url, "info": body.get("info") or {}}
    finally:
        if owns:
            cli.close()


def create_topic(
    client: httpx.Client,
    *,
    x_ua: str,
    title: str,
    contents: list[dict[str, Any]],
    forum_bindings: list[dict[str, Any]],
    image_infos: list[dict[str, Any]] | None = None,
) -> str:
    """建草稿（不公开），返回 draft id_str。"""
    data = _post_form(
        client,
        "moment-draft/v1/create-topic",
        x_ua=x_ua,
        fields=_topic_fields(
            title=title, contents=contents, forum_bindings=forum_bindings, image_infos=image_infos
        ),
    )
    draft = data.get("moment_draft") or {}
    draft_id = draft.get("id_str") or draft.get("id")
    if not draft_id:
        raise TapTapApiError(
            f"create-topic 未返回草稿 id: {json.dumps(data, ensure_ascii=False)[:200]}"
        )
    return str(draft_id)


def publish_topic(
    client: httpx.Client,
    *,
    x_ua: str,
    draft_id: str,
    title: str,
    contents: list[dict[str, Any]],
    forum_bindings: list[dict[str, Any]],
    image_infos: list[dict[str, Any]] | None = None,
) -> str:
    """发布草稿（公开），返回 moment id_str（公开链接 taptap.cn/moment/<id>）。"""
    fields = _topic_fields(
        title=title, contents=contents, forum_bindings=forum_bindings, image_infos=image_infos
    )
    fields["id"] = draft_id
    data = _post_form(client, "moment-draft/v1/publish-topic", x_ua=x_ua, fields=fields)
    moment = data.get("moment") or {}
    moment_id = moment.get("id_str") or moment.get("id")
    if not moment_id:
        raise TapTapApiError(
            f"publish-topic 未返回 moment id: {json.dumps(data, ensure_ascii=False)[:200]}"
        )
    return str(moment_id)


def build_x_ua(vid: int | str, *, uid: str = "00000000-0000-0000-0000-000000000000") -> str:
    """合成 X-UA 查询参。VID（用户 id）必须与登录用户一致，UID（设备 id）任取 uuid 即可
    （实测 spike：服务端身份取自 cookie，X-UA 主要作客户端描述/遥测）。"""
    return (
        "V=1&PN=WebApp&LANG=zh_CN&VN_CODE=102&VN=0.1.0&LOC=CN&PLT=PC&DS=Android"
        f"&UID={uid}&VID={vid}&DT=PC&OS=Windows&OSV=10.0.0"
    )


def get_me(client: httpx.Client, *, x_ua: str) -> dict[str, Any]:
    """GET account-profile/v1/me（只读探针，GET 不需 X-XSRF-TOKEN）。

    返回本人资料 dict（含 ``data.id`` = VID）。cookie 失效 / 未登录抛 TapTapAuthError。
    用途：cookie 体检 + 登录后抽 VID。
    """
    resp = client.get(f"{WEBAPI}/account-profile/v1/me", params={"X-UA": x_ua})
    if resp.status_code in (401, 403):
        raise TapTapAuthError(
            f"TapTap 返回 HTTP {resp.status_code}，cookie 失效，请重新登录", status=resp.status_code
        )
    try:
        body = resp.json()
    except Exception as exc:
        raise TapTapAuthError(
            f"account-profile/v1/me 响应非 JSON (HTTP {resp.status_code})", status=resp.status_code
        ) from exc
    if not body.get("success"):
        raise TapTapAuthError(
            f"account-profile/v1/me 失败: {_error_message(body)}", status=resp.status_code
        )
    data = body.get("data")
    return data if isinstance(data, dict) else {}
