import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from server.app.modules.pipelines.schedule_calc import current_slot, in_window
from server.app.modules.pipelines.service import validate_agent_fields
from server.app.shared.errors import ValidationError

TZ = ZoneInfo("Asia/Shanghai")


def test_validate_ok_minimal():
    validate_agent_fields(
        name="智能体",
        type="general",
        tags=[],
        schedule_kind="none",
        schedule_minute=None,
        schedule_hour=None,
        schedule_weekday=None,
        window_start=None,
        window_end=None,
    )


def test_validate_name_too_long():
    with pytest.raises(ValidationError):
        validate_agent_fields(
            name="x" * 51,
            type="general",
            tags=[],
            schedule_kind="none",
            schedule_minute=None,
            schedule_hour=None,
            schedule_weekday=None,
            window_start=None,
            window_end=None,
        )


def test_validate_bad_type_and_tags():
    with pytest.raises(ValidationError):
        validate_agent_fields(
            name="a",
            type="weird",
            tags=[],
            schedule_kind="none",
            schedule_minute=None,
            schedule_hour=None,
            schedule_weekday=None,
            window_start=None,
            window_end=None,
        )
    with pytest.raises(ValidationError):
        validate_agent_fields(
            name="a",
            type="general",
            tags=["1", "2", "3", "4", "5", "6"],
            schedule_kind="none",
            schedule_minute=None,
            schedule_hour=None,
            schedule_weekday=None,
            window_start=None,
            window_end=None,
        )


def test_validate_schedule_consistency():
    # daily 缺 hour
    with pytest.raises(ValidationError):
        validate_agent_fields(
            name="a",
            type="general",
            tags=[],
            schedule_kind="daily",
            schedule_minute=30,
            schedule_hour=None,
            schedule_weekday=None,
            window_start=None,
            window_end=None,
        )
    # weekly 全齐 OK
    validate_agent_fields(
        name="a",
        type="general",
        tags=[],
        schedule_kind="weekly",
        schedule_minute=30,
        schedule_hour=9,
        schedule_weekday=0,
        window_start=None,
        window_end=None,
    )


def test_validate_window_order():
    with pytest.raises(ValidationError):
        validate_agent_fields(
            name="a",
            type="general",
            tags=[],
            schedule_kind="none",
            schedule_minute=None,
            schedule_hour=None,
            schedule_weekday=None,
            window_start=dt.time(20, 0),
            window_end=dt.time(8, 0),
        )


def test_current_slot_daily_hit_and_miss():
    now = dt.datetime(2026, 6, 5, 9, 30, tzinfo=TZ)
    slot = current_slot("daily", 30, 9, None, now)
    assert slot is not None and slot.hour == 9 and slot.minute == 30
    assert current_slot("daily", 30, 9, None, now.replace(minute=31)) is None
    assert current_slot("daily", 30, 9, None, now.replace(hour=10)) is None


def test_current_slot_hourly_and_weekly():
    now = dt.datetime(2026, 6, 5, 14, 15, tzinfo=TZ)  # 2026-06-05 是周五 → weekday()==4
    assert current_slot("hourly", 15, None, None, now) is not None
    assert current_slot("hourly", 16, None, None, now) is None
    assert current_slot("weekly", 15, 14, 4, now) is not None
    assert current_slot("weekly", 15, 14, 0, now) is None  # 周一


def test_current_slot_none():
    now = dt.datetime(2026, 6, 5, 9, 30, tzinfo=TZ)
    assert current_slot("none", None, None, None, now) is None


def test_in_window():
    now = dt.datetime(2026, 6, 5, 9, 30, tzinfo=TZ)
    assert in_window(None, None, now) is True
    assert in_window(dt.time(7, 0), dt.time(23, 0), now) is True
    assert in_window(dt.time(10, 0), dt.time(23, 0), now) is False
