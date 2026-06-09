"""DailyHotApi 代理：把热榜请求转发给独立 Node 服务（默认 127.0.0.1:6688）。

纯转发、无缓存（缓存交给上游自带的 NodeCache）、无 DB。上游地址读环境变量
GEO_HOTLIST_API_URL，不进 Settings（避免与在途 WIP 改动 config.py 冲突）。
"""

from __future__ import annotations

import contextlib
import os
from typing import Any, AsyncIterator

import httpx

_DEFAULT_BASE_URL = "http://127.0.0.1:6688"
_TIMEOUT_SECONDS = 8.0


class HotListUpstreamError(Exception):
    """上游热榜服务不可用（连接失败 / 超时）。"""


def _base_url() -> str:
    return (os.environ.get("GEO_HOTLIST_API_URL") or _DEFAULT_BASE_URL).rstrip("/")


@contextlib.asynccontextmanager
async def _client_ctx(client: httpx.AsyncClient | None) -> AsyncIterator[httpx.AsyncClient]:
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as owned:
            yield owned


async def fetch_all_sources(client: httpx.AsyncClient | None = None) -> Any:
    url = f"{_base_url()}/all"
    try:
        async with _client_ctx(client) as c:
            resp = await c.get(url)
    except httpx.RequestError as exc:
        raise HotListUpstreamError(str(exc)) from exc
    return resp.json()


async def fetch_source(
    source: str,
    *,
    limit: int | None,
    no_cache: bool,
    client: httpx.AsyncClient | None = None,
) -> tuple[int, Any]:
    params: dict[str, str] = {}
    if limit is not None:
        params["limit"] = str(limit)
    if no_cache:
        params["cache"] = "false"
    url = f"{_base_url()}/{source}"
    try:
        async with _client_ctx(client) as c:
            resp = await c.get(url, params=params)
    except httpx.RequestError as exc:
        raise HotListUpstreamError(str(exc)) from exc
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {"code": resp.status_code, "message": resp.text}
