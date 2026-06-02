from __future__ import annotations

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
from server.app.modules.tasks.drivers.toutiao_html import body_segments_to_toutiao_html

logger = logging.getLogger(__name__)

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
_SECSDK_SETTLE_MS = 2500  # let acrawler/secsdk load + hook the request layer


def _word_count(content_html: str) -> int:
    return len(re.sub(r"<[^>]+>", "", content_html))


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


def _map_publish_response(result: dict[str, Any], title: str) -> PublishResult:
    """Map the in-page XHR result into a PublishResult, or raise.

    Defensive against the exact success shape (the spike captured requests only;
    the live test in Task 6 confirms it). Success predicate: HTTP 200 AND a
    truthy/zero ``code`` with no error message.
    """
    http_status = result.get("httpStatus")
    data = result.get("data")
    if http_status != 200 or not isinstance(data, dict):
        raise PublishError(f"头条发布请求失败: httpStatus={http_status}; raw={result.get('raw')}")
    code = data.get("code")
    if code not in (0, None):
        message = data.get("message") or data.get("msg") or result.get("raw")
        raise PublishError(f"头条发布被拒: code={code}; message={message}")

    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    pgc_id = str(inner.get("pgc_id") or inner.get("id") or "") or None
    url = inner.get("article_url") or inner.get("url")
    return PublishResult(
        url=url or (f"pgc_id={pgc_id}" if pgc_id else None),
        title=title,
        message=f"头条草稿/发布成功: pgc_id={pgc_id}",
    )


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
        content_html = body_segments_to_toutiao_html(payload.body_segments)
        page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(_SECSDK_SETTLE_MS)
        if _is_logged_out(page.url):
            raise UserInputRequired(
                "头条账号未登录或登录态失效，需要人工接管",
                error_type="login_required",
            )
        # Milestone 1 always saves a DRAFT (save=0), which is already a
        # non-publish state, so `stop_before_publish` is intentionally a no-op
        # here. Milestone 2 will flip save=1 and honor the flag by pausing at
        # the preview for manual-confirm.
        form = build_publish_form(title=payload.title, content_html=content_html, save=0)
        result = page.evaluate(_ADAPTER_JS, {"form": form})
        if not isinstance(result, dict):
            raise PublishError(f"头条页内驱动返回意外结果: {result!r}")
        logger.info("toutiao in-page publish raw response: %s", result.get("raw"))
        return _map_publish_response(result, payload.title)


register_variant("toutiao", "inpage", ToutiaoInPageDriver())
