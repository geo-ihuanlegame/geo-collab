"""Capture the editor's OWN save (request body + response) by driving it and
clicking 存草稿. Auto-exits. Resolves: is 7050 our request, or the environment?

  - editor's own save=0 -> code:0  => OUR request is the gap (diff + fix).
  - editor's own save=0 -> code:7050 => environmental (IP/风控), move to production.

    python E:\\geo\\spike_toutiao_editor_save.py
Output -> E:\\geo\\spike_editor_save_capture.json
"""

import json
import os
import sys
from urllib.parse import parse_qsl, urlsplit

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

OUT = "E:/geo/spike_editor_save_capture.json"
PROFILE = os.environ.get(
    "GEO_LIVE_TOUTIAO_PROFILE", r"C:\Users\Administrator\AppData\Local\Temp\geo_spike_userdata"
)
REDACT_PARAM = {"_signature", "a_bogus", "mstoken", "x-bogus"}
records: list[dict] = []


def _redact(v):
    return f"<redacted len={len(v) if v else 0}>"


def _flush():
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"count": len(records), "records": records}, f, ensure_ascii=False, indent=2)


def _on_response(response):
    try:
        req = response.request
        if "/mp/agw/article/publish" not in req.url:
            return
        if req.method != "POST":
            return
        sp = urlsplit(req.url)
        qs = [
            [k, _redact(v) if k.lower() in REDACT_PARAM else v[:50]]
            for k, v in parse_qsl(sp.query, keep_blank_values=True)
        ]
        headers = {
            k.lower(): (_redact(v) if k.lower() == "cookie" else v[:160])
            for k, v in req.headers.items()
            if k.lower() in ("content-type", "x-secsdk-csrf-token", "cookie")
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
                "query": qs,
                "headers": headers,
                "request_body": body,
                "response_status": response.status,
                "response_body": resp_body,
            }
        )
        _flush()
        print(f"[cap] POST /article/publish save -> {response.status} :: {resp_body[:120]}")
    except Exception as e:
        print("rec err:", e)


def _try(fn, label):
    try:
        fn()
        print("ok:", label)
    except Exception as e:
        print("skip:", label, "->", type(e).__name__, str(e)[:100])


def main():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE, headless=False, locale="zh-CN")
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

        _try(
            lambda: page.get_by_role("textbox", name="请输入文章标题").fill("编辑器存稿诊断-请忽略"),
            "fill title",
        )
        page.wait_for_timeout(800)

        def _type_body():
            page.evaluate(
                """() => {
                    const ed = Array.from(document.querySelectorAll("[contenteditable='true']"))
                        .find(el => el.getBoundingClientRect().height >= 80);
                    if (ed) ed.focus();
                }"""
            )
            page.keyboard.type("编辑器自身存稿诊断正文，触发保存。", delay=25)

        _try(_type_body, "type body")
        page.wait_for_timeout(1500)

        # Primary trigger: click 存草稿 / 保存草稿
        clicked = page.evaluate(
            r"""() => {
                const re = /存草稿|保存草稿|存为草稿/;
                const nodes = Array.from(document.querySelectorAll("button,[role='button'],span,div,a"));
                for (const n of nodes) {
                    const t = (n.innerText || n.textContent || "").trim();
                    if (re.test(t) && n.getBoundingClientRect().width > 0) { n.click(); return t; }
                }
                return null;
            }"""
        )
        print("save-button click:", clicked)
        page.wait_for_timeout(8000)  # let save fire

        # Fallback nudge: more typing to trigger debounced autosave
        if not records:
            _try(lambda: page.keyboard.type(" 追加一句。", delay=25), "type more")
            page.wait_for_timeout(9000)

        _flush()
        _try(lambda: ctx.close(), "close")
    print(f"done. {len(records)} save records -> {OUT}")


if __name__ == "__main__":
    main()
