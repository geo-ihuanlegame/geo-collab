"""纯逻辑：判定某 pipeline 在给定本地时刻 now 是否到点、所属 slot；时间窗判定。无 DB。"""

from __future__ import annotations

import datetime as dt


def current_slot(
    kind: str, minute: int | None, hour: int | None, weekday: int | None, now: dt.datetime
) -> dt.datetime | None:
    """now 为带本地时区的 datetime。命中返回截到分钟的 slot datetime，否则 None。"""
    if kind == "hourly":
        if minute is not None and now.minute == minute:
            return now.replace(second=0, microsecond=0)
        return None
    if kind == "daily":
        if minute is not None and hour is not None and now.minute == minute and now.hour == hour:
            return now.replace(second=0, microsecond=0)
        return None
    if kind == "weekly":
        if (
            minute is not None
            and hour is not None
            and weekday is not None
            and now.minute == minute
            and now.hour == hour
            and now.weekday() == weekday
        ):
            return now.replace(second=0, microsecond=0)
        return None
    return None  # none / 未知


def in_window(window_start: dt.time | None, window_end: dt.time | None, now: dt.datetime) -> bool:
    if window_start is None or window_end is None:
        return True
    return window_start <= now.timetz().replace(tzinfo=None) <= window_end
