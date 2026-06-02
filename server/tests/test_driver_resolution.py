import server.app.modules.tasks.drivers.toutiao  # noqa: F401  (registers the real driver)
from server.app.modules.tasks.drivers import (
    get_driver,
    register_variant,
    resolve_driver,
)


class _StubDriver:
    code = "toutiao"
    name = "stub-inpage"
    home_url = "https://mp.toutiao.com"
    publish_url = "https://mp.toutiao.com/x"

    def detect_logged_in(self, *, url, title, body):
        return True

    def publish(self, *, page, context, payload, stop_before_publish):
        raise NotImplementedError


def test_resolve_defaults_to_registered_driver(monkeypatch):
    monkeypatch.delenv("GEO_TOUTIAO_DRIVER", raising=False)
    assert resolve_driver("toutiao") is get_driver("toutiao")


def test_resolve_returns_variant_when_env_set(monkeypatch):
    stub = _StubDriver()
    register_variant("toutiao", "inpage", stub, replace=True)
    monkeypatch.setenv("GEO_TOUTIAO_DRIVER", "inpage")
    assert resolve_driver("toutiao") is stub


def test_resolve_unknown_variant_falls_back(monkeypatch):
    monkeypatch.setenv("GEO_TOUTIAO_DRIVER", "does-not-exist")
    assert resolve_driver("toutiao") is get_driver("toutiao")
