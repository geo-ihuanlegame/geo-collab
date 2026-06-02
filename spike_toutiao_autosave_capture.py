"""Capture the editor's OWN save=0 autosave (full request body + response).

Run it, then in the window that opens:
  1. (If it loaded an existing draft, optionally start a fresh article.)
  2. Type a title + a sentence of body text.
  3. Wait ~15s for the editor to autosave (or click 存草稿 if you see it).
  4. Close the window.

We diff the captured body against our adapter's body to find why save=0 returns
code=7050. Only signature query params + the cookie header are redacted; the
form body (field names/values) is kept — that's what we need.

    conda activate geo_xzpt
    python E:\\geo\\spike_toutiao_autosave_capture.py
Output -> E:\\geo\\spike_autosave_capture.json
"""

import json
import os
import sys
from urllib.parse import parse_qsl, urlsplit

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

OUT = "E:/geo/spike_autosave_capture.json"
PROFILE = os.environ.get(
    "GEO_LIVE_TOUTIAO_PROFILE", r"C:\Users\Administrator\AppData\Local\Temp\geo_spike_userdata"
)
REDACT_PARAM = {"_signature", "a_bogus", "mstoken", "x-bogus"}
records: list[dict] = []


def _redact(v: str) -> str:
    return f"<redacted len={len(v) if v else 0}>"


def _flush() -> None:
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"count": len(records), "records": records}, f, ensure_ascii=False, indent=2)


def _on_response(response) -> None:
    try:
        req = response.request
        if "/mp/agw/article/publish" not in req.url and "/mp/agw/article/edit" not in req.url:
            return
        if req.method != "POST" and "/article/publish" in req.url:
            return
        sp = urlsplit(req.url)
        qs = [
            [k, _redact(v) if k.lower() in REDACT_PARAM else v[:50]]
            for k, v in parse_qsl(sp.query, keep_blank_values=True)
        ]
        headers = {
            k.lower(): (_redact(v) if k.lower() == "cookie" else v[:160])
            for k, v in req.headers.items()
            if k.lower() in ("content-type", "x-secsdk-csrf-token", "referer", "cookie")
        }
        body = None
        try:
            body = req.post_data
        except Exception:
            body = "<unreadable>"
        resp_body = None
        try:
            resp_body = response.text()[:800]
        except Exception:
            resp_body = "<unreadable>"
        records.append(
            {
                "method": req.method,
                "path": sp.path,
                "status": response.status,
                "query": qs,
                "headers": headers,
                "request_body": body,  # full — this is the diff target
                "response_body": resp_body,
            }
        )
        _flush()
        print(f"[cap] {req.method} {sp.path} -> {response.status}")
    except Exception as e:
        print("rec err:", e)


def main() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE, headless=False, locale="zh-CN")
        ctx.on("response", _on_response)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(
                "https://mp.toutiao.com/profile_v4/graphic/publish",
                wait_until="domcontentloaded",
                timeout=60000,
            )
        except Exception as e:
            print("nav:", e)
        print("=" * 70)
        print("在编辑器里输入标题+一句正文 -> 等约15s自动保存(或点存草稿) -> 关闭窗口")
        print("捕获写入:", OUT)
        print("=" * 70)
        try:
            while ctx.pages:
                ctx.pages[0].wait_for_timeout(1000)
        except Exception:
            pass
        _flush()
        try:
            ctx.close()
        except Exception:
            pass
    print(f"done. {len(records)} records -> {OUT}")


if __name__ == "__main__":
    main()
