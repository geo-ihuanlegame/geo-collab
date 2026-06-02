"""M2 Phase 2.0 capture: record Toutiao COVER + BODY image upload traffic.

Goal: capture the (currently un-captured) image-upload contract so Phase 2.1+
can be authored without guessing. We need, end-to-end:
  - the upload endpoint(s) + method (ByteDance ImageX is often a 3-step
    ApplyImageUpload -> PUT/POST to a store host -> CommitImageUpload),
  - the returned `tos-cn-i-...` uri shape,
  - the `/mp/agw/article_material/photo/info` resolve call (uris -> image info),
  - how the cover uri lands in `pgc_feed_covers` and how a body image lands as
    an `<img>` in the publish `content`.

Why a NEW sibling (not an edit to spike_toutiao_m2_capture.py): the m2_capture
probe matches ONLY toutiao.com/bytedance.com hosts, so it would silently MISS
the imagex / upload-CDN hosts where the bytes actually go. This script broadens
the matcher to those hosts AND sniffs any JSON/text response whose body contains
`tos-cn-i-`, so the upload call is captured no matter which host serves it.

Run on a CLEAN network (secsdk healthy) with a logged-in profile, headed; then
MANUALLY upload a cover + one body image in the opened editor. The script keeps
capturing until you close the window (or ~6 min elapse) and flushes after every
hit, so it is interrupt-safe.

    python E:\\geo\\spike_toutiao_image_capture.py
Output -> E:\\geo\\spike_image_capture.json

Sensitive values (cookies / signatures / tokens) are REDACTED; business fields
(uris, codes, field names, image-info) are kept — that's what we need.
"""

import json
import os
import sys
from urllib.parse import parse_qsl, urlsplit

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

OUT = "E:/geo/spike_image_capture.json"
PROFILE = os.environ.get(
    "GEO_LIVE_TOUTIAO_PROFILE", r"C:\Users\Administrator\AppData\Local\Temp\geo_spike_userdata"
)

# Toutiao/bytedance article-layer endpoints (publish form, photo/info resolve, …)
PATH_HINTS = (
    "article", "draft", "publish", "pgc", "cover", "photo", "material",
    "upload", "imagex", "create", "save",
)
# Upload / CDN hosts where the image BYTES actually go. The m2_capture probe
# misses these because it requires a toutiao.com/bytedance.com host. ImageX and
# the TOS store hosts live on these domains instead.
HOST_HINTS = (
    "imagex", "byteimg", "ibyteimg", "vcloud", "snssdk", "pstatp",
    "bytedanceapi", "zijieapi", "volces", "volcengine", "tos-",
)
# Tells us a response carried an upload result even if the host/path looked plain.
TOS_MARKER = "tos-cn-i-"

REDACT_PARAM = {"_signature", "a_bogus", "mstoken", "x-bogus"}
REDACT_HEADER = {"cookie", "authorization", "x-bogus"}
KEEP_HEADER = {"content-type", "x-secsdk-csrf-token", "x-tt-csrf", "referer"}

records: list[dict] = []


def _redact(v: str) -> str:
    return f"<redacted len={len(v) if v else 0}>"


def _url_interesting(url: str) -> bool:
    """True if the URL alone marks this as upload/article traffic worth keeping."""
    sp = urlsplit(url)
    host = (sp.hostname or "").lower()
    low = sp.path.lower()
    q = sp.query.lower()
    if ("toutiao.com" in host or "bytedance.com" in host) and any(h in low for h in PATH_HINTS):
        return True
    if any(h in host for h in HOST_HINTS):
        return True
    # ImageX action endpoints are query-driven and may sit on a bare host.
    if "imageupload" in q or "applyimage" in q or "commitimage" in q or "imagex" in low:
        return True
    return False


def _req_content_type(req) -> str:
    for k, v in req.headers.items():
        if k.lower() == "content-type":
            return v.lower()
    return ""


def _request_body_summary(req) -> str | None:
    """Form/JSON bodies recorded verbatim (truncated); multipart/binary summarized."""
    ctype = _req_content_type(req)
    if "multipart/form-data" in ctype:
        size = None
        try:
            buf = req.post_data_buffer  # bytes; safe for binary
            size = len(buf) if buf is not None else None
        except Exception:
            size = None
        return f"<multipart {size if size is not None else '?'} bytes; ctype={ctype[:160]}>"
    try:
        pd = req.post_data
        return pd[:3000] if pd else None
    except Exception:
        return "<unreadable/binary>"


def _flush() -> None:
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"count": len(records), "records": records}, f, ensure_ascii=False, indent=2)


def _on_response(response) -> None:
    try:
        req = response.request
        url = req.url
        cap = _url_interesting(url)
        body = None
        if not cap:
            # Sniff only JSON/text bodies for the tos marker (skip image binaries).
            ctype = (response.headers or {}).get("content-type", "").lower()
            if "json" in ctype or "text" in ctype or "xml" in ctype:
                try:
                    body = response.text()
                except Exception:
                    body = None
                if body and TOS_MARKER in body:
                    cap = True
            if not cap:
                return
        if body is None:
            try:
                body = response.text()
            except Exception:
                body = "<unreadable/binary>"

        sp = urlsplit(url)
        qs = parse_qsl(sp.query, keep_blank_values=True)
        params = [[k, _redact(v) if k.lower() in REDACT_PARAM else v[:80]] for k, v in qs]
        headers = {}
        for k, v in req.headers.items():
            lk = k.lower()
            if lk in KEEP_HEADER or lk in REDACT_HEADER:
                headers[lk] = _redact(v) if lk in REDACT_HEADER else v[:200]

        records.append(
            {
                "method": req.method,
                "host": sp.hostname,
                "path": sp.path,
                "status": response.status,
                "resp_content_type": (response.headers or {}).get("content-type", ""),
                "query_param_names": [k for k, _ in qs],
                "sig_present": sorted(
                    {k for k, _ in qs if k.lower() in ("_signature", "a_bogus", "mstoken", "x-bogus")}
                ),
                "csrf_header_present": "x-secsdk-csrf-token" in headers,
                "query_params": params,
                "request_headers": headers,
                "request_body": _request_body_summary(req),
                "response_body": (body or "")[:4000],
                "tos_in_response": bool(body and TOS_MARKER in body),
            }
        )
        _flush()
        marker = " [TOS]" if (body and TOS_MARKER in body) else ""
        print(f"[cap] {req.method} {sp.hostname}{sp.path} -> {response.status}{marker}")
    except Exception as e:
        print("rec err:", e)


def _try(fn, label):
    try:
        fn()
        print("ok:", label)
    except Exception as e:
        print("skip:", label, "->", type(e).__name__, str(e)[:120])


def main() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, headless=False, locale="zh-CN", viewport={"width": 1440, "height": 900}
        )
        ctx.on("response", _on_response)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        _try(
            lambda: page.goto(
                "https://mp.toutiao.com/profile_v4/graphic/publish",
                wait_until="domcontentloaded",
                timeout=60000,
            ),
            "goto editor",
        )
        page.wait_for_timeout(6000)
        print("final_url:", page.url)

        print("\n=== 手动步骤（在弹出的浏览器里操作） ===")
        print("1) 给标题随便填几个字（建一个草稿）。")
        print("2) 点封面区域 -> 本地上传一张封面图，等 “已上传 1 张图片” 后确定。")
        print("3) 在正文里插入一张本地图片。")
        print("4) 等约 10s 让自动保存/预览触发，然后直接关闭浏览器窗口即可。")
        print("脚本在持续抓包：imagex/上传 CDN 主机、/photo/info、以及任何含 tos-cn-i- 的响应。\n")

        # Keep capturing until the operator closes the window, or ~6 min elapse.
        for _ in range(72):  # 72 * 5s = 360s
            if not ctx.pages:
                break
            try:
                page.wait_for_timeout(5000)
            except Exception:
                break
            _flush()

        _flush()
        _try(lambda: ctx.close(), "close ctx")
    print(f"done. {len(records)} records -> {OUT}")
    print("Next: distill the upload endpoint/method, tos-uri shape, photo/info call,")
    print("      and pgc_feed_covers / body <img> wiring into the design doc §M2.")


if __name__ == "__main__":
    main()
