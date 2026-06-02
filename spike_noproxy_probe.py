"""Test the proxy hypothesis: run the in-page save with the system proxy BYPASSED.

- If it connects AND the save no longer returns 7050 -> the proxy/IP was the cause
  (our request is fine; 7050 is environmental 风控).
- If it cannot connect at all -> this machine needs the proxy to reach Toutiao, so
  the clean-network test must happen in production/Docker instead.

    python E:\\geo\\spike_noproxy_probe.py
"""

import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PROFILE = os.environ.get(
    "GEO_LIVE_TOUTIAO_PROFILE", r"C:\Users\Administrator\AppData\Local\Temp\geo_spike_userdata"
)

from server.app.modules.articles.parser import BodySegment  # noqa: E402
from server.app.modules.tasks.drivers.base import PublishError, PublishPayload  # noqa: E402
from server.app.modules.tasks.drivers.toutiao_inpage import ToutiaoInPageDriver  # noqa: E402


def main() -> None:
    from playwright.sync_api import sync_playwright

    payload = PublishPayload(
        title="无代理探针-请忽略",
        cover_asset_path=Path("unused.png"),
        body_segments=[BodySegment(kind="text", text="无代理探针正文。")],
        account_key="np",
        state_path=Path("unused.json"),
        display_name="np",
        platform_code="toutiao",
    )
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE,
            headless=False,
            locale="zh-CN",
            args=["--no-proxy-server"],  # ignore the system proxy, connect directly
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            r = ToutiaoInPageDriver().publish(
                page=page, context=ctx, payload=payload, stop_before_publish=True
            )
            print("RESULT = SAVE OK (no 7050!):", r.message)
        except PublishError as e:
            print("RESULT = PublishError:", str(e)[:220])
        except Exception as e:
            print("RESULT = could-not-run:", type(e).__name__, str(e)[:220])
        finally:
            try:
                ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
