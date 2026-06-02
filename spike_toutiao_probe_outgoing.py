"""Definitive probe: what does OUR in-page XHR actually send?

Runs the real ToutiaoInPageDriver against the logged-in profile, but attaches a
request listener to capture the OUTGOING POST /mp/agw/article/publish — so we can
see exactly which signing params (a_bogus/msToken/_signature) and which headers
(notably x-secsdk-csrf-token) the global hook added vs. what the real editor sends.

This tells us why the save returns code=7050.
    python E:\\geo\\spike_toutiao_probe_outgoing.py
"""

import os
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

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

captured: list[dict] = []


def _on_request(req) -> None:
    if "/mp/agw/article/publish" not in req.url:
        return
    sp = urlsplit(req.url)
    qs = [k for k, _ in parse_qsl(sp.query, keep_blank_values=True)]
    hdr_names = sorted(k.lower() for k in req.headers.keys())
    captured.append(
        {
            "query_param_names": qs,
            "has_a_bogus": "a_bogus" in qs,
            "has_msToken": "msToken" in qs,
            "has_signature": "_signature" in qs,
            "header_names": hdr_names,
            "has_secsdk_csrf": "x-secsdk-csrf-token" in hdr_names,
            "has_tt_csrf": "x-tt-csrf" in hdr_names,
        }
    )


def main() -> None:
    from playwright.sync_api import sync_playwright

    payload = PublishPayload(
        title="探针-请忽略",
        cover_asset_path=Path("unused.png"),
        body_segments=[BodySegment(kind="text", text="探针正文。")],
        account_key="probe",
        state_path=Path("unused.json"),
        display_name="probe",
        platform_code="toutiao",
    )
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE, headless=False, locale="zh-CN")
        ctx.on("request", _on_request)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            ToutiaoInPageDriver().publish(
                page=page, context=ctx, payload=payload, stop_before_publish=True
            )
            print("publish returned (unexpected success)")
        except PublishError as e:
            print("PublishError (expected if 7050):", str(e)[:200])
        except Exception as e:
            print("other error:", type(e).__name__, str(e)[:200])
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    print("=== OUTGOING /mp/agw/article/publish requests ===")
    for c in captured:
        print("query:", c["query_param_names"])
        print(
            f"  a_bogus={c['has_a_bogus']} msToken={c['has_msToken']} "
            f"_signature={c['has_signature']}"
        )
        print(
            f"  x-secsdk-csrf-token={c['has_secsdk_csrf']} x-tt-csrf={c['has_tt_csrf']}"
        )
        print("  headers:", c["header_names"])
    if not captured:
        print("(no /article/publish request captured)")


if __name__ == "__main__":
    main()
