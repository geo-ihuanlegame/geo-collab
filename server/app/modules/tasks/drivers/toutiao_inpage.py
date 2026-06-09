"""头条号「页内」发布驱动（variant=inpage）。

不走 DOM 操作，而是把正文序列化成 HTML、图片 base64 编码后，整体交给页内 JS 适配器
（adapters/toutiao_publish.js）在浏览器上下文里直接调头条官方上传 / 发布接口——
借页面自带的 secsdk 请求签名钩子完成鉴权。注册为 toutiao 的 inpage 变体，
由 GEO_TOUTIAO_DRIVER=inpage 启用，与默认 DOM 驱动并存便于灰度回滚。
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any

from server.app.modules.tasks.drivers import register_variant
from server.app.modules.tasks.drivers.base import (
    PublishError,
    PublishPayload,
    PublishResult,
    UserInputRequired,
)
from server.app.modules.tasks.drivers.image_upload import _maybe_resize_for_upload
from server.app.modules.tasks.drivers.toutiao_html import (
    ImageRef,
    body_segments_to_toutiao_html,
)

logger = logging.getLogger(__name__)

# 图片上传端点。单次 multipart POST 在页面内完成，让页面的全局 secsdk hook
# 为请求签名（字段 `file` = 图片 Blob）。响应携带已上传图片的 uri
# （tos-cn-i-…）、url 和 width/height。
UPLOAD_URL = (
    "https://mp.toutiao.com/mp/agw/article_material/photo/upload_picture"
    "?type=ueditor&pgc_watermark=1&action=uploadimage&encode=utf-8"
)
# 发布端点（form-urlencoded）。save=1 + entrance=main 表示真实发布；
# save=0 表示草稿。通过 arg.publishUrl 传给 JS。
PUBLISH_API_URL = (
    "https://mp.toutiao.com/mp/agw/article/publish"
    "?source=mp&type=article&aid=1231&mp_publish_ab_val=0"
)

_EXTRA_BASE = {
    "content_source": 100000000402,
    "is_multi_title": 0,
    "sub_titles": [],
    "gd_ext": {
        "entrance": "",
        "from_page": "publisher_mp",
        "enter_from": "PC",
        "device_platform": "mp",
        "is_message": 0,
    },
    "tuwen_wtt_transfer_switch": "1",
}

PUBLISH_URL = "https://mp.toutiao.com/profile_v4/graphic/publish"
_ADAPTER_JS = (Path(__file__).parent / "adapters" / "toutiao_publish.js").read_text(
    encoding="utf-8"
)
# 已确认登出重定向目标是 mp.toutiao.com/auth/page/login（2026-06-02 spike）；
# 其它值是防御性匹配。避免裸 "login" 子串，防止误判。
_LOGIN_HINTS = ("/auth/page/login", "passport", "sso.toutiao.com")
# 编辑器标题框 placeholder：它出现即表示“已登录 + 编辑器就绪”，同时说明
# acrawler/secsdk 请求 hook 已加载。
_EDITOR_TITLE_PLACEHOLDER = "请输入文章标题"


def _word_count(content_html: str) -> int:
    return len(re.sub(r"<[^>]+>", "", content_html))


_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _b64_of(path: Path) -> tuple[str, str]:
    """读取图片（必要时降采样）并返回 (base64-ascii, mime)。

    复用共享上传瘦身器，让过大图片先降采样成 JPEG，再 base64 编入 page.evaluate
    参数。
    """
    with _maybe_resize_for_upload(path) as resolved:
        data = resolved.read_bytes()
        mime = _MIME_BY_SUFFIX.get(resolved.suffix.lower(), "image/jpeg")
    return base64.b64encode(data).decode("ascii"), mime


def _build_evaluate_arg(
    payload: PublishPayload,
    content_html: str,
    image_order: list[ImageRef],
    save: int,
) -> dict[str, Any]:
    """组装单个 page.evaluate 参数：form + base64 图片 + 端点。"""
    form = build_publish_form(title=payload.title, content_html=content_html, save=save)

    cover: dict[str, str] | None = None
    if payload.cover_asset_path is not None:
        b64, mime = _b64_of(payload.cover_asset_path)
        cover = {"b64": b64, "mime": mime}

    body_images: list[dict[str, str]] = []
    for ref in image_order:
        if ref.image_path is None:
            raise PublishError(f"头条正文图片缺少本地路径: token={ref.token}")
        b64, mime = _b64_of(ref.image_path)
        body_images.append({"token": ref.token, "b64": b64, "mime": mime})

    return {
        "form": form,
        "cover": cover,
        "bodyImages": body_images,
        "uploadUrl": UPLOAD_URL,
        "publishUrl": PUBLISH_API_URL,
    }


def build_publish_form(
    *,
    title: str,
    content_html: str,
    save: int = 0,
    pgc_id: str | None = None,
) -> dict[str, str]:
    """构建发布调用所需的 application/x-www-form-urlencoded 字段。

    常量对齐 2026-06-02 抓到的真实编辑器请求（见设计文档 §6「Spike 结论 · phase 2」）。
    Milestone 1 发送 save=0（草稿），不带封面。
    """
    extra = dict(_EXTRA_BASE)
    extra["content_word_cnt"] = _word_count(content_html)

    form: dict[str, str] = {
        "source": "29",
        "extra": json.dumps(extra, ensure_ascii=False, separators=(",", ":")),
        "content": content_html,
        "title": title,
        "search_creation_info": json.dumps(
            {"searchTopOne": 0, "abstract": "", "clue_id": ""}, separators=(",", ":")
        ),
        "mp_editor_stat": "{}",
        "is_refute_rumor": "0",
        "save": str(save),
        "entrance": "main" if save == 1 else "",
        "draft_form_data": json.dumps({"coverType": 2}, separators=(",", ":")),
        "pgc_feed_covers": "[]",
        "article_ad_type": "3",
        "is_fans_article": "0",
        "govern_forward": "0",
        "praise": "0",
        "disable_praise": "0",
        "tree_plan_article": "0",
        "claim_exclusive": "0",
        "timer_status": "0",
    }
    if pgc_id:
        form["pgc_id"] = pgc_id
    return form


def _is_logged_out(url: str) -> bool:
    return any(hint in url for hint in _LOGIN_HINTS)


def _wait_editor_ready(page: Any, timeout_ms: int = 15000) -> bool:
    """编辑器标题框出现时返回 True；登录墙持续存在时返回 False。

    容忍 goto 后的短暂重定向：通过轮询编辑器标题框判断，而不是只看一次 URL
    （曾捕捉到瞬时重定向并误抛 UserInputRequired）。只有超时后仍停在登录 URL
    才判定为未登录。
    """
    waited, step = 0, 500
    while waited < timeout_ms:
        try:
            if page.get_by_role("textbox", name=_EDITOR_TITLE_PLACEHOLDER).count() > 0:
                return True
        except Exception:
            pass
        page.wait_for_timeout(step)
        waited += step
    return not _is_logged_out(page.url)


def _map_publish_response(
    result: dict[str, Any], title: str, *, is_draft: bool = False
) -> PublishResult:
    """把页内 XHR 结果映射成 PublishResult，或抛出异常。

    发布响应结构只有中等置信度（M1 spike 只抓到了请求）。成功条件：HTTP 200，
    且 ``code in (0, None)``，并且没有错误消息。URL 提取按 ``article_url`` →
    ``url`` → ``display_url`` 回退；pgc_id 按 ``pgc_id`` → ``id`` 回退。
    ``data.data`` 缺失或不是 dict 时不崩溃，而是返回温和结果（url 为 None /
    无 pgc_id），不抛异常。

    ``is_draft`` 选择消息文案（保存草稿或真实发布）；它
    不影响成功判定与 URL 提取。
    """
    http_status = result.get("httpStatus")
    data = result.get("data")
    if http_status != 200 or not isinstance(data, dict):
        raise PublishError(f"头条发布请求失败: httpStatus={http_status}; raw={result.get('raw')}")
    code = data.get("code")
    if code not in (0, None):
        message = data.get("message") or data.get("msg") or result.get("raw")
        raise PublishError(f"头条发布被拒: code={code}; message={message}")

    raw_inner = data.get("data")
    inner: dict[str, Any] = raw_inner if isinstance(raw_inner, dict) else {}
    pgc_id = str(inner.get("pgc_id") or inner.get("id") or "") or None
    url = inner.get("article_url") or inner.get("url") or inner.get("display_url")
    if is_draft:
        message = f"头条草稿已保存（待手动确认发布）: pgc_id={pgc_id}"
    else:
        message = f"头条发布成功: pgc_id={pgc_id}"
    return PublishResult(
        url=url or (f"pgc_id={pgc_id}" if pgc_id else None),
        title=title,
        message=message,
    )


def _map_full_response(result: Any, title: str, *, is_draft: bool = False) -> PublishResult:
    """把单轮往返信封映射成 PublishResult，或抛出异常。

    信封结构（来自 adapters/toutiao_publish.js）：
      success:     {ok:true,  step:"publish", uploads:[...], publish:{...}}
      upload fail: {ok:false, step:"upload", index, httpStatus, raw}

    ``is_draft`` 会传给 ``_map_publish_response``，让成功消息区分草稿和发布。
    """
    if not isinstance(result, dict):
        raise PublishError(f"头条页内驱动返回意外结果: {result!r}")
    if result.get("ok") is False and result.get("step") == "upload":
        raise PublishError(
            f"头条图片上传失败: index={result.get('index')} "
            f"httpStatus={result.get('httpStatus')} field={result.get('field')} "
            f"b64len={result.get('b64len')} raw={result.get('raw')}"
        )
    publish = result.get("publish")
    if not isinstance(publish, dict):
        raise PublishError(f"头条页内驱动缺少发布结果: {result!r}")
    return _map_publish_response(publish, title, is_draft=is_draft)


class ToutiaoInPageDriver:
    code = "toutiao"
    name = "头条号(页内)"
    home_url = "https://mp.toutiao.com"
    publish_url = PUBLISH_URL

    def detect_logged_in(self, *, url: str, title: str, body: str) -> bool:
        if _is_logged_out(url):
            return False
        return "mp.toutiao.com" in url

    def publish(
        self,
        *,
        page: Any,
        context: Any,
        payload: PublishPayload,
        stop_before_publish: bool,
    ) -> PublishResult:
        """通过页内适配器发布（或保存草稿）文章。

        默认（``stop_before_publish=False``）是全自动 ``save=1`` 真实发布；必须有
        封面图，非零 API code 会抛 ``PublishError``。当 ``stop_before_publish=True``
        时，适配器发送 ``save=0``（仅草稿），结果消息说明草稿已保存、等待人工确认；
        executor 随后自行把记录停在 ``waiting_manual_publish``。本驱动不设置记录状态，
        只正常返回。封面和正文图片不论 ``save`` 值都会上传；``save`` 只控制发布调用的
        save/entrance 字段。

        注意：让 manual-confirm 真正重新发布已保存草稿（设计 §10 option A/B）是
        M2 之后的任务；当前只保存草稿。
        """
        content_html, image_order = body_segments_to_toutiao_html(payload.body_segments)
        page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
        if not _wait_editor_ready(page):
            raise UserInputRequired(
                "头条账号未登录或登录态失效，需要人工接管",
                error_type="login_required",
            )
        # save=0 -> 草稿（遵守 stop_before_publish）；save=1 -> 真实发布。
        # 封面和正文图片不论 save 值都会上传；save 只控制发布调用的 save/entrance 字段。
        save = 0 if stop_before_publish else 1
        is_draft = save == 0
        if save == 1 and payload.cover_asset_path is None:
            raise PublishError("头条发布需要封面图片")
        arg = _build_evaluate_arg(payload, content_html, image_order, save)
        result = page.evaluate(_ADAPTER_JS, arg)
        if isinstance(result, dict):
            logger.info(
                "toutiao in-page publish: uploads=%s publish.raw=%s",
                result.get("uploads"),
                (result.get("publish") or {}).get("raw")
                if isinstance(result.get("publish"), dict)
                else None,
            )
        return _map_full_response(result, payload.title, is_draft=is_draft)


register_variant("toutiao", "inpage", ToutiaoInPageDriver())
