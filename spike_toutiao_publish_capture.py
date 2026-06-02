"""SPIKE phase 2 (login-required): capture ONE real Toutiao publish request.

Run this YOURSELF in a terminal (it opens a real browser window you drive):

    conda activate geo_xzpt
    python E:\\geo\\spike_toutiao_publish_capture.py

In the window that opens:
  1. Log in to your Toutiao account (scan QR) if prompted.
  2. Write a short THROWAWAY article + cover, click 预览并发布 -> 确认发布.
  3. When it shows published, CLOSE the browser window. (You can delete the test
     article from your account afterwards.)

Output -> E:\\geo\\spike_publish_capture.json
Sensitive values (cookies / signatures / tokens) are REDACTED — only param &
header NAMES and request shapes are kept, which is all we need to lock the
signing scheme. The browser profile is stored in your OS temp dir, NOT the repo.
"""

import json
import os
import sys
import tempfile
from urllib.parse import parse_qsl, urlsplit

try:  # avoid GBK UnicodeEncodeError when printing Chinese on a Windows console
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

OUT = "E:/geo/spike_publish_capture.json"
USER_DATA_DIR = os.path.join(tempfile.gettempdir(), "geo_spike_userdata")

# query-param / header names whose VALUE we redact but whose presence we keep
REDACT_PARAM = {"_signature", "a_bogus", "mstoken", "x-bogus", "mstoken"}
REDACT_HEADER = {"cookie", "authorization", "x-tt-csrf", "x-secsdk-csrf-token", "x-bogus"}
SIG_PARAM_NAMES = {"_signature", "a_bogus", "mstoken", "msToken", "x-bogus"}
KEEP_HEADERS = {
    "content-type", "x-tt-csrf", "x-secsdk-csrf-token", "x-bogus", "referer",
    "x-requested-with", "origin",
}
PUBLISH_HINTS = (
    "publish", "article", "save", "pgc", "content", "graphic", "create",
    "submit", "draft",
)

captures: list[dict] = []


def _redact(val: str) -> str:
    return f"<redacted len={len(val) if val else 0}>"


def _looks_publishy(url: str, method: str) -> bool:
    host = urlsplit(url).hostname or ""
    if not any(host.endswith(h) for h in (
        "toutiao.com", "snssdk.com", "bytedance.com", "zijieapi.com"
    )):
        return False
    low = url.lower()
    if any(k in low for k in ("_signature", "a_bogus", "mstoken", "x-bogus")):
        return True
    if method == "POST" and any(h in low for h in PUBLISH_HINTS):
        return True
    return False


def _flush() -> None:
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"count": len(captures), "captures": captures}, f,
                  ensure_ascii=False, indent=2)


def _record(req) -> None:
    try:
        url, method = req.url, req.method
        if not _looks_publishy(url, method):
            return
        sp = urlsplit(url)
        qs = parse_qsl(sp.query, keep_blank_values=True)
        params = [
            [k, _redact(v) if k.lower() in REDACT_PARAM else (v[:80])]
            for k, v in qs
        ]
        sig_present = sorted({k for k, _ in qs if k in SIG_PARAM_NAMES})
        headers = {}
        for k, v in req.headers.items():
            lk = k.lower()
            if lk in KEEP_HEADERS or lk in REDACT_HEADER:
                headers[lk] = _redact(v) if lk in REDACT_HEADER else v[:160]
        body = None
        try:
            pd = req.post_data
            body = pd[:1500] if pd else None
        except Exception:
            body = "<unreadable>"
        captures.append({
            "method": method,
            "host": sp.hostname,
            "path": sp.path,
            "sig_params_present": sig_present,
            "query_param_names": [k for k, _ in qs],
            "query_params": params,
            "interesting_headers": headers,
            "post_body_head": body,
        })
        _flush()
        print(f"[captured] {method} {sp.hostname}{sp.path}  sig={sig_present}")
    except Exception as e:  # never let capture crash the session
        print("record error:", e)


def main() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        ctx.on("request", _record)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(
                "https://mp.toutiao.com/profile_v4/graphic/publish",
                wait_until="domcontentloaded", timeout=60000,
            )
        except Exception as e:
            print("nav:", e)
        print("=" * 72)
        print("登录(扫码) -> 写一篇测试文+封面 -> 预览并发布 -> 确认发布 -> 关闭窗口")
        print("捕获实时写入:", OUT)
        print("=" * 72)
        try:
            while ctx.pages:
                ctx.pages[0].wait_for_timeout(1000)
        except Exception:
            pass  # window closed
        _flush()
        try:
            ctx.close()
        except Exception:
            pass
    print(f"done. {len(captures)} captures -> {OUT}")


if __name__ == "__main__":
    main()
