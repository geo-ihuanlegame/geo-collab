import asyncio

import httpx

from server.app.modules.hot_lists import service


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_fetch_all_sources_forwards_to_all():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"code": 200, "count": 2, "routes": []})

    result = asyncio.run(service.fetch_all_sources(client=_client(handler)))
    assert captured["url"].endswith("/all")
    assert result["count"] == 2


def test_fetch_source_passes_limit_and_cache_and_returns_status():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/weibo"
        assert request.url.params.get("limit") == "10"
        assert request.url.params.get("cache") == "false"
        return httpx.Response(200, json={"code": 200, "name": "weibo", "data": []})

    status, payload = asyncio.run(
        service.fetch_source("weibo", limit=10, no_cache=True, client=_client(handler))
    )
    assert status == 200
    assert payload["name"] == "weibo"


def test_fetch_source_maps_request_error_to_upstream_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    try:
        asyncio.run(
            service.fetch_source("weibo", limit=None, no_cache=False, client=_client(handler))
        )
    except service.HotListUpstreamError:
        return
    raise AssertionError("expected HotListUpstreamError")
