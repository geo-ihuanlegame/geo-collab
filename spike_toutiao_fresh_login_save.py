"""Test whether a FRESH login restores secsdk (real CSRF) and lets the save go green.

Uses a brand-new profile dir (forces fresh QR login + fresh secsdk handshake,
like phase-2 which saved successfully). After you scan the QR, it runs our
in-page adapter's save and reports: green vs 7050, and whether
x-secsdk-csrf-token is a real token or the DOWNGRADE placeholder.

    python E:\\geo\\spike_toutiao_fresh_login_save.py
"""

import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

FRESH_PROFILE = os.environ.get(
    "GEO_FRESH_PROFILE", r"C:\Users\Administrator\AppData\Local\Temp\geo_fresh_userdata"
)

from server.app.modules.articles.parser import BodySegment  # noqa: E402
from server.app.modules.tasks.drivers.base import PublishError, PublishPayload  # noqa: E402
from server.app.modules.tasks.drivers.toutiao_inpage import ToutiaoInPageDriver  # noqa: E402

csrf_seen = {"value": None}


def _on_request(req):
    if "/mp/agw/article/publish" in req.url and req.method == "POST":
        tok = req.headers.get("x-secsdk-csrf-token")
        if tok is not None:
            csrf_seen["value"] = "DOWNGRADE" if tok == "DOWNGRADE" else f"<real token len={len(tok)}>"


def main():
    from playwright.sync_api import sync_playwright

    payload = PublishPayload(
        title="全新登录存稿测试-请忽略",
        cover_asset_path=Path("unused.png"),
        body_segments=[BodySegment(kind="text", text="全新登录后的存稿测试正文。")],
        account_key="fresh",
        state_path=Path("unused.json"),
        display_name="fresh",
        platform_code="toutiao",
    )
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(FRESH_PROFILE, headless=False, locale="zh-CN")
        ctx.on("request", _on_request)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(
                "https://mp.toutiao.com/profile_v4/graphic/publish",
                wait_until="domcontentloaded",
                timeout=60000,
            )
        except Exception as e:
            print("nav:", e)

        print("=" * 66)
        print("请在窗口里扫码登录头条号（最多等 240s，登录后会自动跑保存）...")
        print("=" * 66)
        logged_in = False
        for i in range(240):
            # Positive signal: the editor's title box only exists once logged in
            # and on the editor page (survives the JS redirect to /auth/page/login).
            try:
                if page.get_by_role("textbox", name="请输入文章标题").count() > 0:
                    logged_in = True
                    break
            except Exception:
                pass
            if i % 15 == 0:
                try:
                    print(f"  ...等待登录中 ({i}s) url={page.url[:80]}")
                except Exception:
                    pass
            page.wait_for_timeout(1000)

        if not logged_in:
            print("RESULT = 未检测到登录完成（超时）。final_url:", page.url)
            try:
                ctx.close()
            except Exception:
                pass
            return

        print("已登录，运行页内适配器保存...")
        page.wait_for_timeout(2000)
        try:
            r = ToutiaoInPageDriver().publish(
                page=page, context=ctx, payload=payload, stop_before_publish=True
            )
            print("RESULT = SAVE OK (green!):", r.message)
        except PublishError as e:
            print("RESULT = PublishError:", str(e)[:200])
        except Exception as e:
            print("RESULT = other:", type(e).__name__, str(e)[:200])
        finally:
            print("x-secsdk-csrf-token on our save =", csrf_seen["value"])
            try:
                ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
