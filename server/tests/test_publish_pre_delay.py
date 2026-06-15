from types import SimpleNamespace

from server.app.core.config import Settings
from server.app.modules.tasks import executor


def test_pre_delay_defaults(monkeypatch):
    for var in (
        "GEO_PUBLISH_PRE_DELAY_ENABLED",
        "GEO_PUBLISH_PRE_DELAY_MIN_SECONDS",
        "GEO_PUBLISH_PRE_DELAY_MAX_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert s.publish_pre_delay_enabled is True
    assert s.publish_pre_delay_min_seconds == 10.0
    assert s.publish_pre_delay_max_seconds == 120.0


def test_pre_delay_env_override(monkeypatch):
    monkeypatch.setenv("GEO_PUBLISH_PRE_DELAY_ENABLED", "false")
    monkeypatch.setenv("GEO_PUBLISH_PRE_DELAY_MIN_SECONDS", "5")
    monkeypatch.setenv("GEO_PUBLISH_PRE_DELAY_MAX_SECONDS", "30")
    s = Settings()
    assert s.publish_pre_delay_enabled is False
    assert s.publish_pre_delay_min_seconds == 5.0
    assert s.publish_pre_delay_max_seconds == 30.0


def _fake_settings(**kw):
    base = dict(
        publish_record_timeout_seconds=300,
        publish_pre_delay_enabled=True,
        publish_pre_delay_min_seconds=10.0,
        publish_pre_delay_max_seconds=120.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_budget_extended_when_enabled(monkeypatch):
    monkeypatch.setattr(
        executor, "get_settings", lambda: _fake_settings(publish_pre_delay_enabled=True)
    )
    assert executor._record_execution_budget() == 420.0


def test_budget_base_when_disabled(monkeypatch):
    monkeypatch.setattr(
        executor, "get_settings", lambda: _fake_settings(publish_pre_delay_enabled=False)
    )
    assert executor._record_execution_budget() == 300


def test_delay_called_within_range(monkeypatch):
    monkeypatch.setattr(executor, "get_settings", lambda: _fake_settings())
    calls = []
    executor._maybe_pre_publish_delay(
        SimpleNamespace(id=7),
        False,
        sleep=lambda d: calls.append(d),
        rng=lambda lo, hi: (lo + hi) / 2,
    )
    assert calls == [65.0]


def test_delay_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(
        executor, "get_settings", lambda: _fake_settings(publish_pre_delay_enabled=False)
    )
    calls = []
    executor._maybe_pre_publish_delay(SimpleNamespace(id=1), False, sleep=lambda d: calls.append(d))
    assert calls == []


def test_delay_skipped_when_stop_before_publish(monkeypatch):
    monkeypatch.setattr(executor, "get_settings", lambda: _fake_settings())
    calls = []
    executor._maybe_pre_publish_delay(SimpleNamespace(id=1), True, sleep=lambda d: calls.append(d))
    assert calls == []


def test_delay_clamps_when_min_gt_max(monkeypatch):
    monkeypatch.setattr(
        executor,
        "get_settings",
        lambda: _fake_settings(
            publish_pre_delay_min_seconds=200.0, publish_pre_delay_max_seconds=120.0
        ),
    )
    seen = {}

    def fake_rng(lo, hi):
        seen["lo"], seen["hi"] = lo, hi
        return lo

    executor._maybe_pre_publish_delay(
        SimpleNamespace(id=1), False, sleep=lambda d: None, rng=fake_rng
    )
    assert seen == {"lo": 200.0, "hi": 200.0}
