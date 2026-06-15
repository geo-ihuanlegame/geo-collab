"""每日分组「边生成边进组」共用 helper（ai_generate / ai_compose 共用）。

make_group_streamer(ctx, cfg)：daily_group 开启时先建好当天「每日生成 · 日期」分组，
返回 (group_id, stream_fn)；关闭或建组失败 → (None, no-op)（退回非流式、不丢文章）。
stream_fn(aid)：把该篇标 pending + 追加进当天组。sort_order 用进程内计数器
（threading.Lock 只护内存自增；DB 追加在锁外并发、各自不同行，无 DB 锁竞争——见
docs/superpowers/specs/2026-06-15-streaming-daily-group-design.md §7）。"""

import datetime as dt
import itertools
import logging
import threading
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def make_group_streamer(ctx, cfg):
    if not cfg.get("daily_group"):
        return None, (lambda _aid: None)

    from server.app.core.config import get_settings
    from server.app.modules.articles.service import (
        append_article_to_group_pending,
        resolve_or_create_daily_group,
    )

    today = dt.datetime.now(ZoneInfo(get_settings().scheduler_tz)).date()
    group_name = f"每日生成 · {today:%Y-%m-%d}"
    resolved = resolve_or_create_daily_group(
        ctx.session_factory, user_id=ctx.user_id, group_name=group_name
    )
    if resolved is None:
        logger.warning("daily_group 建组失败，退回非流式：%s", group_name)
        return None, (lambda _aid: None)

    group_id, next_start = resolved
    counter = itertools.count(next_start)
    lock = threading.Lock()

    def _stream(aid: int) -> None:
        with lock:
            so = next(counter)
        ok = append_article_to_group_pending(
            ctx.session_factory, group_id=group_id, article_id=aid, sort_order=so
        )
        if not ok:
            # best-effort：文章已生成、不会从 article_ids 丢失，但没进组——记一条警告便于排查
            logger.warning("daily_group 追加失败（article=%s group=%s）", aid, group_id)

    return group_id, _stream
