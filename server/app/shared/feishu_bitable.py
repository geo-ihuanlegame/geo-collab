"""飞书多维表格(Bitable)读客户端。

用 GEO_FEISHU_APP_ID / GEO_FEISHU_APP_SECRET 换 tenant_access_token（带缓存+到期刷新），
读取多维表记录（自动翻页）。问题库同步、以及第二阶段"发布采集写回"都复用此模块。

注意：字段值保持飞书原样（同步层忠实镜像），不在此处展平 —— 取法后置到生文拼接时处理。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from threading import Lock
from typing import Any

from server.app.core.config import get_settings

_logger = logging.getLogger(__name__)
_FEISHU_BASE = "https://open.feishu.cn/open-apis"

# 模块级 token 缓存（tenant_access_token 有效期约 7200s）
_token_cache: dict[str, Any] = {"token": None, "expire_at": 0.0}
_token_lock = Lock()

# 飞书 token 失效错误码
_TOKEN_INVALID_CODES = {99991663, 99991661, 99991664}


class FeishuError(Exception):
    """飞书 API 调用失败（配置缺失 / 网络 / 业务错误码）。"""


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    body: dict | None = None,
    timeout: int = 15,
) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:500]
        raise FeishuError(f"飞书 HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise FeishuError(f"飞书网络错误: {exc.reason}") from exc


def get_tenant_access_token(force: bool = False) -> str:
    settings = get_settings()
    app_id = settings.feishu_app_id
    app_secret = settings.feishu_app_secret
    if not app_id or not app_secret:
        raise FeishuError("未配置 GEO_FEISHU_APP_ID / GEO_FEISHU_APP_SECRET")

    with _token_lock:
        now = time.time()
        if not force and _token_cache["token"] and now < float(_token_cache["expire_at"]):
            return str(_token_cache["token"])

        resp = _http_json(
            "POST",
            f"{_FEISHU_BASE}/auth/v3/tenant_access_token/internal",
            body={"app_id": app_id, "app_secret": app_secret},
        )
        if resp.get("code") != 0:
            raise FeishuError(
                f"获取 tenant_access_token 失败: code={resp.get('code')} msg={resp.get('msg')}"
            )
        token = str(resp["tenant_access_token"])
        expire = int(resp.get("expire", 7200))
        _token_cache["token"] = token
        _token_cache["expire_at"] = now + expire - 120  # 提前 2 分钟刷新
        return token


def list_bitable_records(app_token: str, table_id: str, *, page_size: int = 500) -> list[dict]:
    """拉取多维表全部记录（自动翻页）。返回 [{"record_id": str, "fields": dict}, ...]。"""
    token = get_tenant_access_token()
    items: list[dict] = []
    page_token: str | None = None

    while True:
        url = (
            f"{_FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records"
            f"?page_size={page_size}"
        )
        if page_token:
            url += f"&page_token={page_token}"

        resp = _http_json("GET", url, headers={"Authorization": f"Bearer {token}"})
        if resp.get("code") in _TOKEN_INVALID_CODES:
            token = get_tenant_access_token(force=True)
            resp = _http_json("GET", url, headers={"Authorization": f"Bearer {token}"})
        if resp.get("code") != 0:
            raise FeishuError(f"读取多维表失败: code={resp.get('code')} msg={resp.get('msg')}")

        data = resp.get("data", {}) or {}
        for item in data.get("items", []) or []:
            items.append({"record_id": item.get("record_id"), "fields": item.get("fields", {})})

        if data.get("has_more") and data.get("page_token"):
            page_token = data["page_token"]
        else:
            break

    return items
