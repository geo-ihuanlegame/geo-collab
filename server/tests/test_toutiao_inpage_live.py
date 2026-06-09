import os
from pathlib import Path

import pytest

from server.app.modules.articles.parser import BodySegment
from server.app.modules.tasks.drivers.base import PublishPayload
from server.app.modules.tasks.drivers.toutiao_inpage import ToutiaoInPageDriver

pytestmark = pytest.mark.live

PROFILE = os.environ.get("GEO_LIVE_TOUTIAO_PROFILE")


@pytest.mark.skipif(not PROFILE, reason="set GEO_LIVE_TOUTIAO_PROFILE to a logged-in user-data-dir")
def test_live_draft_save_round_trip():
    from playwright.sync_api import sync_playwright

    payload = PublishPayload(
        title="架构验证草稿-请忽略",
        cover_asset_path=Path("unused.png"),
        body_segments=[
            BodySegment(kind="text", text="这是页内适配器架构验证草稿。"),
            BodySegment(kind="text", text="\n"),
            BodySegment(kind="text", text="第二段，含", bold=False),
            BodySegment(kind="text", text="加粗", bold=True),
        ],
        account_key="live",
        state_path=Path("unused.json"),
        display_name="live",
        platform_code="toutiao",
    )
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE, headless=False, locale="zh-CN")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            # save=0 草稿不需要封面（已通过 spike 抓包确认）
            result = ToutiaoInPageDriver().publish(
                page=page, context=ctx, payload=payload, stop_before_publish=True
            )
        finally:
            ctx.close()
    # 驱动会记录原始响应；成功时 result.message 携带 pgc_id。
    assert result.title == "架构验证草稿-请忽略"
    assert "成功" in result.message
