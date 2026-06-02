"""SPIKE (login-less): detect Toutiao request-signing architecture.

Decision gate for the in-page adapter design:
  - If fetch / XMLHttpRequest are globally patched by a signing SDK (acrawler /
    webmssdk), an in-page fetch() we call via page.evaluate() would ALSO be
    auto-signed  ->  pure adapter is viable.
  - If signing is inline in app code, a raw in-page fetch() is unsigned
    ->  hybrid (DOM for the final signed publish call).

This does NOT log in or publish. It only loads public pages and inspects JS.
"""

import json

from playwright.sync_api import sync_playwright

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

PROBE_JS = r"""() => {
    const isNative = (fn) => {
        try { return /\{\s*\[native code\]\s*\}/.test(Function.prototype.toString.call(fn)); }
        catch (e) { return 'err:' + e.message; }
    };
    const srcOf = (fn) => {
        try { return Function.prototype.toString.call(fn).slice(0, 400); }
        catch (e) { return 'err:' + e.message; }
    };
    const scripts = Array.from(document.scripts).map(s => s.src).filter(Boolean);
    const interesting = scripts.filter(
        s => /acrawler|webmssdk|secsdk|sec-sdk|frontier|sign|bdms|slardar|tt_player|byted/i.test(s)
    );
    return {
        fetch_native: isNative(window.fetch),
        xhr_open_native: isNative(XMLHttpRequest.prototype.open),
        xhr_send_native: isNative(XMLHttpRequest.prototype.send),
        fetch_src: srcOf(window.fetch),
        xhr_send_src: srcOf(XMLHttpRequest.prototype.send),
        has_byted_acrawler: typeof window.byted_acrawler,
        has_webmssdk: typeof window.webmssdk,
        sign_globals: Object.keys(window).filter(
            k => /acrawler|webmssdk|secsdk|byted|_sign|bytedance|bdms/i.test(k)
        ),
        cookie_has_ac: /__ac_nonce|__ac_signature/.test(document.cookie),
        cookie_keys: document.cookie.split(';').map(c => c.trim().split('=')[0]).filter(Boolean),
        interesting_scripts: interesting,
        total_scripts: scripts.length,
    };
}"""


def probe_url(page, url, reqs):
    out = {"target": url}
    reqs.clear()
    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
    except Exception as e:
        out["nav_error"] = repr(e)
    page.wait_for_timeout(3000)
    out["final_url"] = page.url
    try:
        out["title"] = page.title()
    except Exception as e:
        out["title"] = "err:" + str(e)
    try:
        out["probe"] = page.evaluate(PROBE_JS)
    except Exception as e:
        out["probe"] = "err:" + repr(e)

    sig_keys = ["_signature", "a_bogus", "msToken", "X-Bogus", "x-bogus", "_signature="]
    signed = [r for r in reqs if any(k in r for k in sig_keys)]
    out["signed_requests_sample"] = signed[:12]
    out["total_requests"] = len(reqs)
    out["request_hosts"] = sorted({r.split("/")[2] for r in reqs if "://" in r})
    return out


def main():
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            user_agent=UA, viewport={"width": 1440, "height": 900}, locale="zh-CN"
        )
        page = ctx.new_page()
        reqs: list[str] = []
        page.on("request", lambda r: reqs.append(r.url))

        # mp.toutiao.com = the actual publish domain (redirects to login when logged out,
        # but the signing SDK still loads + patches globals on this domain).
        results["mp"] = probe_url(
            page, "https://mp.toutiao.com/profile_v4/graphic/publish", reqs
        )
        # www.toutiao.com makes REAL signed API calls even logged-out (the content feed),
        # so it shows the signing mechanism in action.
        results["www"] = probe_url(page, "https://www.toutiao.com/", reqs)

        browser.close()

    with open("E:/geo/spike_result.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("OK: wrote spike_result.json")


if __name__ == "__main__":
    main()
