"""热榜代理路由：/api/hot-lists（列出全部源）、/api/hot-lists/{source}（取某源）。

鉴权在 main.py 注册时统一加（dependencies=[Depends(get_current_user)]）。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from . import service

router = APIRouter()

_SOURCE_RE = re.compile(r"^[a-z0-9-]+$")


@router.get("")
async def list_sources():
    try:
        return await service.fetch_all_sources()
    except service.HotListUpstreamError as exc:
        raise HTTPException(status_code=502, detail="热榜服务不可用") from exc


@router.get("/{source}")
async def get_source(
    source: str,
    limit: int | None = Query(default=None, ge=1, le=500),
    cache: bool = Query(default=True),
):
    if not _SOURCE_RE.match(source):
        raise HTTPException(status_code=400, detail="非法的榜单名")
    try:
        status_code, payload = await service.fetch_source(source, limit=limit, no_cache=not cache)
    except service.HotListUpstreamError as exc:
        raise HTTPException(status_code=502, detail="热榜服务不可用") from exc
    return JSONResponse(status_code=status_code, content=payload)
