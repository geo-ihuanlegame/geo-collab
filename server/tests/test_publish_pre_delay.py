from server.app.core.config import Settings


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
