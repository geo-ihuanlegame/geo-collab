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
    t = now.timetz().replace(tzinfo=None)
    if window_start <= window_end:
        return window_start <= t <= window_end
    return t >= window_start or t <= window_end  # 跨午夜


def last_due_slot(
    kind: str, minute: int | None, hour: int | None, weekday: int | None, now: dt.datetime
) -> dt.datetime | None:
    """返回 <= now 的最近一个计划 slot（截到分钟）；none/未配置返回 None。
    与 current_slot 不同：不要求 now 恰好落在计划分钟，从而轮询漂移 / 间隔>60s 也不漏跑，
    由调度器结合 last_scheduled_run_at claim 去重保证每个 slot 只触发一次。
    依赖 GEO_SCHEDULER_TZ 为无 DST 时区（如 Asia/Shanghai）。"""
    if kind == "hourly":
        if minute is None:
            return None
        slot = now.replace(minute=minute, second=0, microsecond=0)
        if slot > now:
            slot -= dt.timedelta(hours=1)
        return slot
    if kind == "daily":
        if minute is None or hour is None:
            return None
        slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if slot > now:
            slot -= dt.timedelta(days=1)
        return slot
    if kind == "weekly":
        if minute is None or hour is None or weekday is None:
            return None
        days_back = (now.weekday() - weekday) % 7
        slot = (now - dt.timedelta(days=days_back)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if slot > now:
            slot -= dt.timedelta(days=7)
        return slot
    return None
