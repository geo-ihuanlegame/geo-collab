"""M2 capture: record the editor's OWN draft-init + autosave (request AND response)
so we can diff against our failing save (code=7050).

Runs time-boxed and auto-exits (no manual close needed). Uses the logged-in
profile at GEO_LIVE_TOUTIAO_PROFILE. Drives a minimal edit (title + one body
paragraph) to trigger the editor's debounced autosave, capturing every
mp.toutiao.com article/draft/cover/publish call with its response body.

Sensitive values (cookies / signatures / tokens) are REDACTED; business fields
(pgc_id, error codes, field names) are kept — that's what we need.

    python E:\\geo\\spike_toutiao_m2_capture.py
Output -> E:\\geo\\spike_m2_capture.json
"""

import json
import os
import sys
from urllib.parse import parse_qsl, urlsplit

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

OUT = "E:/geo/spike_m2_capture.json"
PROFILE = os.environ.get(
    "GEO_LIVE_TOUTIAO_PROFILE", r"C:\Users\Administrator\AppData\Local\Temp\geo_spike_userdata"
)

PATH_HINTS = (
    "article", "draft", "publish", "pgc", "cover", "photo", "material",
    "upload", "imagex", "create", "save",
)
REDACT_PARAM = {"_signature", "a_bogus", "mstoken", "x-bogus"}
REDACT_HEADER = {"cookie", "authorization", "x-bogus"}
KEEP_HEADER = {"content-type", "x-secsdk-csrf-token", "x-tt-csrf", "referer"}

records: list[dict] = []


def _redact(v: str) -> str:
    return f"<redacted len={len(v) if v else 0}>"


def _interesting(url: str) -> bool:
    sp = urlsplit(url)
    host = sp.hostname or ""
    if "toutiao.com" not in host and "bytedance.com" not in host:
        return False
    low = sp.path.lower()
    return any(h in low for h in PATH_HINTS)


def _flush() -> None:
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"count": len(records), "records": records}, f, ensure_ascii=False, indent=2)


def _on_response(response) -> None:
    try:
        req = response.request
        url = req.url
        if not _interesting(url):
            return
        sp = urlsplit(url)
        qs = parse_qsl(sp.query, keep_blank_values=True)
        params = [
            [k, _redact(v) if k.lower() in REDACT_PARAM else v[:60]] for k, v in qs
        ]
        headers = {}
        for k, v in req.headers.items():
            lk = k.lower()
            if lk in KEEP_HEADER or lk in REDACT_HEADER:
                headers[lk] = _redact(v) if lk in REDACT_HEADER else v[:160]
        post = None
        try:
            pd = req.post_data
            post = pd[:2500] if pd else None
        except Exception:
            post = "<unreadable>"
        body = None
        try:
            body = response.text()[:1500]
        except Exception:
            body = "<unreadable>"
        records.append(
            {
                "method": req.method,
                "host": sp.hostname,
                "path": sp.path,
                "status": response.status,
                "query_param_names": [k for k, _ in qs],
                "sig_present": sorted(
                    {k for k, _ in qs if k in ("_signature", "a_bogus", "msToken", "x-bogus")}
                ),
                "csrf_header_present": "x-secsdk-csrf-token" in headers,
                "query_params": params,
                "request_headers": headers,
                "request_post_head": post,
                "response_body_head": body,
            }
        )
        _flush()
        print(f"[cap] {req.method} {sp.path} -> {response.status}")
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
        page.wait_for_timeout(6000)  # capture draft-init on load
        print("final_url:", page.url)

        # Drive a minimal edit to trigger the editor's own autosave.
        _try(
            lambda: page.get_by_role("textbox", name="请输入文章标题").fill("M2诊断标题-请忽略"),
            "fill title",
        )
        page.wait_for_timeout(1500)

        def _type_body():
            editor = page.locator("[contenteditable='true']").first
            editor.click()
            page.keyboard.type("M2 诊断正文，触发自动保存。", delay=20)

        _try(_type_body, "type body")
        page.wait_for_timeout(12000)  # let debounced autosave fire

        # Nudge another autosave
        _try(lambda: page.keyboard.type(" 第二句。", delay=20), "type more")
        page.wait_for_timeout(10000)

        _flush()
        _try(lambda: ctx.close(), "close ctx")
    print(f"done. {len(records)} records -> {OUT}")


if __name__ == "__main__":
    main()
