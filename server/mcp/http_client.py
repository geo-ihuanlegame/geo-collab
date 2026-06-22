"""GEO API HTTP client (sync).

设计要点:
- 默认走 httpx.Client（FastMCP tool handler 是 sync 的，async client 反而麻烦）
- 自动在所有请求注入 `X-MCP-Token` header
- 4xx/5xx 一律抛 ApiError(描述包含 method/path/status/detail)，让 tool handler 转成
  {ok: false, error: ...} 顶层封装返回给 LLM
- 大对象（如 article content）按 GEO 现有 schema 返回，不做裁剪——LLM 决定要不要二次读
"""

from __future__ import annotations

from typing import Any

import httpx


class ApiError(Exception):
    """非 2xx 响应或网络错误。"""


class GeoApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={"X-MCP-Token": token},
        )

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, *, json: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, json=json)

    def patch(self, path: str, *, json: dict[str, Any] | None = None) -> Any:
        return self._request("PATCH", path, json=json)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        try:
            resp = self._client.request(method, url, params=params, json=json)
        except httpx.RequestError as exc:
            raise ApiError(f"{method} {path}: network error: {exc}") from exc
        if resp.status_code >= 400:
            detail = _extract_detail(resp)
            raise ApiError(f"{method} {path}: {resp.status_code} {detail}")
        try:
            return resp.json()
        except ValueError:
            return resp.text


def _extract_detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(data, dict):
        return str(data.get("detail") or data.get("message") or data)[:300]
    return str(data)[:300]
