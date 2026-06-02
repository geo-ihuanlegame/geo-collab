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

# Image-upload endpoint. A single multipart POST done IN-PAGE so the page's
# global secsdk hook signs it (field `file` = the image Blob). The response
# carries the uploaded image's uri (tos-cn-i-…) + url + width/height.
UPLOAD_URL = (
    "https://mp.toutiao.com/mp/agw/article_material/photo/upload_picture"
    "?type=ueditor&pgc_watermark=1&action=uploadimage&encode=utf-8"
)
# Publish endpoint (form-urlencoded). save=1 + entrance=main = real publish;
# save=0 = draft. Passed to the JS via arg.publishUrl.
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
# Confirmed logout redirect target is mp.toutiao.com/auth/page/login (spike 2026-06-02);
# the others are defensive. Avoid a bare "login" substring — it false-positives.
_LOGIN_HINTS = ("/auth/page/login", "passport", "sso.toutiao.com")
# Editor title box placeholder — its presence is our "logged-in + editor ready"
# signal (also implies the acrawler/secsdk request hook has loaded).
_EDITOR_TITLE_PLACEHOLDER = "请输入文章标题"


def _word_count(content_html: str) -> int:
    return len(re.sub(r"<[^>]+>", "", content_html))


_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _b64_of(path: Path) -> tuple[str, str]:
    """Read an image (downscaled if needed) as (base64-ascii, mime).

    Reuses the shared upload resizer so oversized images are downscaled to JPEG
    before being base64-encoded into the page.evaluate arg.
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
    """Assemble the single page.evaluate arg: form + base64 images + endpoints."""
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
    """Build the application/x-www-form-urlencoded fields for the publish call.

    Constants mirror the real editor request captured 2026-06-02 (see design doc
    §6 "Spike 结论 · phase 2"). Milestone 1 sends save=0 (draft) with no cover.
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
    """True once the editor (title box) is present; False if a login wall persists.

    Tolerates a transient post-goto redirect: poll for the editor title box
    instead of judging login state from the URL in a single shot (which caught
    a momentary redirect and wrongly raised UserInputRequired). Only concludes
    logged-out if the login URL is still showing after the timeout.
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
    """Map the in-page XHR result into a PublishResult, or raise.

    The publish *response* shape is medium-confidence (the M1 spike captured
    requests only). Success predicate: HTTP 200 AND ``code in (0, None)`` with no
    error message. URL extraction falls back ``article_url`` → ``url`` →
    ``display_url``; pgc_id falls back ``pgc_id`` → ``id``. Never crashes when
    ``data.data`` is missing or non-dict — a missing inner payload yields a
    graceful result (url None / pgc_id-less) rather than an exception.

    ``is_draft`` selects the message wording (draft saved vs. real publish); it
    does NOT affect the success predicate or URL extraction.
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
    """Map the single-round-trip envelope into a PublishResult, or raise.

    Envelope (from adapters/toutiao_publish.js):
      success:     {ok:true,  step:"publish", uploads:[...], publish:{...}}
      upload fail: {ok:false, step:"upload", index, httpStatus, raw}

    ``is_draft`` is threaded through to ``_map_publish_response`` so the success
    message reflects draft-vs-publish.
    """
    if not isinstance(result, dict):
        raise PublishError(f"头条页内驱动返回意外结果: {result!r}")
    if result.get("ok") is False and result.get("step") == "upload":
        raise PublishError(
            f"头条图片上传失败: index={result.get('index')} "
            f"httpStatus={result.get('httpStatus')} raw={result.get('raw')}"
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
        """Publish (or draft) an article via the in-page adapter.

        Default (``stop_before_publish=False``) is a full-auto ``save=1`` real
        publish; a cover image is required and a non-zero API code raises
        ``PublishError``. With ``stop_before_publish=True`` the adapter sends
        ``save=0`` (draft only) and the result message says a draft was saved
        awaiting manual confirm; the executor then parks the record at
        ``waiting_manual_publish`` on its own — this driver does NOT set record
        status, it just returns normally. Cover + body images upload regardless
        of ``save``; ``save`` only controls the publish call's save/entrance
        fields.

        NOTE: making manual-confirm actually re-publish the saved draft (design
        §10 option A/B) is a LATER, out-of-M2 task — today a draft is just saved.
        """
        content_html, image_order = body_segments_to_toutiao_html(payload.body_segments)
        page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
        if not _wait_editor_ready(page):
            raise UserInputRequired(
                "头条账号未登录或登录态失效，需要人工接管",
                error_type="login_required",
            )
        # save=0 -> draft (honors stop_before_publish); save=1 -> real publish.
        # Cover + body images upload REGARDLESS of save; save only controls the
        # publish call's save/entrance fields.
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
