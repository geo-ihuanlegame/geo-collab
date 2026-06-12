# 微信公众号平台接入（草稿箱发布）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把微信公众号登记为第二个发布平台：账号凭据管理（AppID/AppSecret + token 缓存）、纯 HTTP 草稿箱发布（封面/正文图自动压缩转传），完整融入 Platform/Account/Task/PublishRecord/pipeline 体系。

**Architecture:** 驱动注册表扩「API 驱动」（驱动声明 `mode="api"`），`build_publish_runner_for_record` 在进浏览器路径前分叉到新的 `runner_api.run_publish_api`（不碰 Playwright）。spike 逻辑移植为 `wechat_client.py` 纯函数（httpx 同步 + 注入式 client）+ `wechat_images.py`（Pillow 压缩纯函数）+ `wechat_mp.py` 薄驱动。token 的 DB 读写在 runner 侧（驱动不碰 ORM）。

**Tech Stack:** FastAPI + SQLAlchemy/Alembic（MySQL only）、httpx（同步 Client + MockTransport 测试）、Pillow 12、pytest（`build_test_app` + `pytest.mark.mysql`）。

**Spec:** `docs/superpowers/specs/2026-06-10-wechat-mp-platform-design.md`

---

## 环境准备（每个执行者先做一次）

```bash
# 工具 shell 里 conda activate 不生效，解析 geo_xzpt 环境 python 全路径，后文以 $PY 指代
PY=$(conda run -n geo_xzpt python -c "import sys;print(sys.executable)")
# 跑 DB 用例需要（DB 名必须含 test）：
export GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test"
```

约定：所有 pytest 命令均为 `"$PY" -m pytest ...`，在仓库根目录 `e:\geo` 执行。

## 并行执行分组（subagent 调度用）

| 波次 | 任务 | 依赖 |
|---|---|---|
| **W1（三个可并行）** | Task 1（迁移+模型）、Task 2（wechat_client）、Task 3（wechat_images） | 无 |
| **W2（两个可并行）** | Task 4（账号 schemas/service/router + 测试）、Task 5（distribute 过滤） | Task 4 ← 1,2；Task 5 ← 1 |
| **W3（串行）** | Task 6（payload + 驱动 + runner_api + executor 分叉 + 注册） | 1,2,3 |
| **W4（串行收口）** | Task 7（CLAUDE.md + 全量回归） | 全部 |

并行写文件零冲突：W1 三个任务各自只动互不重叠的文件。Task 4 与 Task 5 文件也不重叠。

---

### Task 1: 数据库迁移 + Account/模型字段

**Files:**
- Create: `server/alembic/versions/0044_wechat_mp_accounts.py`
- Modify: `server/app/modules/accounts/models.py:38-57`（Account 列）
- Modify: `server/app/modules/accounts/schemas.py:15-27,85-99`（AccountRead.state_path 改可空 + 新字段 + to_account_read）
- Test: 复用 `server/tests/test_fts_and_migrations.py`（迁移链自动验证），新增断言不必需

> 注意：`schemas.py` 在本 task 只做「state_path 可空 + 新只读字段」的最小改动，
> 让 `to_account_read` 不因 NULL state_path 崩；请求体类在 Task 4 加。

- [ ] **Step 1: 改 Account 模型**

`server/app/modules/accounts/models.py`：`sqlalchemy` import 行加 `JSON`，Account 类内：

```python
    state_path: Mapped[str | None] = mapped_column(
        String(1000), nullable=True
    )  # Playwright storage_state.json 的相对路径；API 型账号（如公众号）为 NULL
    # ── API 型平台（公众号等）专用 ───────────────────────────────────────
    api_credentials: Mapped[dict | None] = mapped_column(
        JSON, nullable=True
    )  # {"app_id": ..., "app_secret": ...}；永不通过 API 回传原文
    api_token_cache: Mapped[dict | None] = mapped_column(
        JSON, nullable=True
    )  # {"access_token": ..., "expires_at": <epoch秒>}；web/worker 跨进程共享
    # ── 通用账号字段（对齐媒体矩阵交互稿）─────────────────────────────────
    distribution_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", index=True
    )  # 分发开关：False 时 pipeline distribute 自动派号跳过该账号
    contact: Mapped[str | None] = mapped_column(String(200), nullable=True)  # 绑定联系方式
    avatar_asset_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("assets.id"), nullable=True
    )  # 账号头像
```

（替换原 `state_path` 定义；其余列保持原位不动。）

- [ ] **Step 2: 写迁移**

`server/alembic/versions/0044_wechat_mp_accounts.py`：

```python
"""账号表支持 API 型平台（微信公众号）：凭据/token 缓存/分发开关/联系方式/头像；种入 wechat_mp 平台

修订 ID: 0044
上一修订: 0043
创建日期: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("api_credentials", sa.JSON(), nullable=True))
    op.add_column("accounts", sa.Column("api_token_cache", sa.JSON(), nullable=True))
    op.add_column(
        "accounts",
        sa.Column(
            "distribution_enabled", sa.Boolean(), nullable=False, server_default="1"
        ),
    )
    op.create_index(
        "ix_accounts_distribution_enabled", "accounts", ["distribution_enabled"]
    )
    op.add_column("accounts", sa.Column("contact", sa.String(200), nullable=True))
    op.add_column("accounts", sa.Column("avatar_asset_id", sa.String(64), nullable=True))
    op.create_foreign_key(
        "fk_accounts_avatar_asset_id", "accounts", "assets", ["avatar_asset_id"], ["id"]
    )
    op.alter_column(
        "accounts", "state_path", existing_type=sa.String(1000), nullable=True
    )
    # 幂等种入 wechat_mp 平台
    conn = op.get_bind()
    exists = conn.execute(
        sa.text("SELECT id FROM platforms WHERE code = 'wechat_mp'")
    ).first()
    if exists is None:
        conn.execute(
            sa.text(
                "INSERT INTO platforms (code, name, base_url, enabled, created_at) "
                "VALUES ('wechat_mp', '微信公众号', 'https://mp.weixin.qq.com', 1, NOW())"
            )
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM platforms WHERE code = 'wechat_mp'"))
    op.alter_column(
        "accounts", "state_path", existing_type=sa.String(1000), nullable=False
    )
    op.drop_constraint("fk_accounts_avatar_asset_id", "accounts", type_="foreignkey")
    op.drop_column("accounts", "avatar_asset_id")
    op.drop_column("accounts", "contact")
    op.drop_index("ix_accounts_distribution_enabled", table_name="accounts")
    op.drop_column("accounts", "distribution_enabled")
    op.drop_column("accounts", "api_token_cache")
    op.drop_column("accounts", "api_credentials")
```

- [ ] **Step 3: AccountRead 兼容可空 state_path + 暴露新字段**

`server/app/modules/accounts/schemas.py` — `AccountRead` 改：

```python
class AccountRead(BaseModel):
    id: int
    platform_code: str
    platform_name: str
    display_name: str
    platform_user_id: str | None
    status: str  # 状态：valid / expired / unknown
    last_checked_at: datetime | None
    last_login_at: datetime | None
    state_path: str | None  # Playwright storage_state.json 路径；API 型账号为 None
    note: str | None
    contact: str | None = None  # 绑定联系方式
    avatar_asset_id: str | None = None
    distribution_enabled: bool = True
    app_id: str | None = None  # API 型账号的 AppID（明文）
    app_secret_tail: str | None = None  # AppSecret 尾 4 位掩码；原文永不回传
    created_at: datetime
    updated_at: datetime
```

`to_account_read` 改：

```python
def to_account_read(account: "Account") -> AccountRead:
    creds = account.api_credentials or {}
    secret = creds.get("app_secret") or ""
    return AccountRead(
        id=account.id,
        platform_code=account.platform.code,
        platform_name=account.platform.name,
        display_name=account.display_name,
        platform_user_id=account.platform_user_id,
        status=account.status,
        last_checked_at=account.last_checked_at,
        last_login_at=account.last_login_at,
        state_path=account.state_path,
        note=account.note,
        contact=account.contact,
        avatar_asset_id=account.avatar_asset_id,
        distribution_enabled=account.distribution_enabled,
        app_id=creds.get("app_id"),
        app_secret_tail=secret[-4:] if secret else None,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )
```

- [ ] **Step 4: 跑迁移 + 现有账号测试回归**

```bash
"$PY" -m alembic upgrade head
"$PY" -m pytest server/tests/test_fts_and_migrations.py server/tests/test_accounts_api.py -q
```

Expected: PASS（既有用例不受可空 state_path 影响；若有用例断言 `state_path: str` 非空，按可空语义修正该用例）。

- [ ] **Step 5: Commit**

```bash
git add server/alembic/versions/0044_wechat_mp_accounts.py server/app/modules/accounts/models.py server/app/modules/accounts/schemas.py
git commit -m "feat(accounts): 账号表支持 API 型平台——凭据/token缓存/分发开关/联系方式/头像 + 种入 wechat_mp"
```

---

### Task 2: 微信 HTTP 客户端（纯函数，无 DB）

**Files:**
- Create: `server/app/modules/tasks/drivers/wechat_client.py`
- Test: `server/tests/test_wechat_client.py`

设计要点：所有函数显式收 `client: httpx.Client`（测试注入 `MockTransport`）；不读环境变量、不碰 ORM；errcode 非 0 抛 `WeChatApiError`（`40164` 附 IP 白名单提示）。

- [ ] **Step 1: 写失败测试**

`server/tests/test_wechat_client.py`：

```python
"""wechat_client 纯函数测试：httpx.MockTransport 打桩，无 DB、无网络。"""

import json

import httpx
import pytest

from server.app.modules.tasks.drivers.wechat_client import (
    WeChatApiError,
    add_draft,
    build_draft_article,
    fetch_access_token,
    upload_content_image,
    upload_thumb,
)


def make_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_access_token_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/cgi-bin/token"
        assert request.url.params["appid"] == "wx1"
        assert request.url.params["secret"] == "s1"
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})

    token, expires_in = fetch_access_token("wx1", "s1", client=make_client(handler))
    assert token == "tok"
    assert expires_in == 7200


def test_fetch_access_token_error_40164_appends_whitelist_hint():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 40164, "errmsg": "invalid ip"})

    with pytest.raises(WeChatApiError) as exc_info:
        fetch_access_token("wx1", "s1", client=make_client(handler))
    assert exc_info.value.errcode == 40164
    assert "IP 白名单" in str(exc_info.value)


def test_fetch_access_token_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(WeChatApiError):
        fetch_access_token("wx1", "s1", client=make_client(handler))


def test_upload_thumb_returns_media_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/cgi-bin/material/add_material"
        assert request.url.params["type"] == "thumb"
        assert request.url.params["access_token"] == "tok"
        assert b"cover.jpg" in request.read()
        return httpx.Response(200, json={"media_id": "m1", "url": "http://x"})

    media_id = upload_thumb("tok", "cover.jpg", b"\xff\xd8jpegbytes", client=make_client(handler))
    assert media_id == "m1"


def test_upload_content_image_returns_url():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/cgi-bin/media/uploadimg"
        return httpx.Response(200, json={"url": "https://mmbiz.qpic.cn/x.jpg"})

    url = upload_content_image("tok", "body.png", b"\x89PNGbytes", client=make_client(handler))
    assert url == "https://mmbiz.qpic.cn/x.jpg"


def test_add_draft_returns_media_id_and_posts_utf8():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read().decode("utf-8"))
        return httpx.Response(200, json={"media_id": "draft1"})

    article = build_draft_article(title="标题", content_html="<p>正文</p>", thumb_media_id="m1")
    media_id = add_draft("tok", article, client=make_client(handler))
    assert media_id == "draft1"
    sent = captured["body"]["articles"][0]
    assert sent["title"] == "标题"
    assert sent["thumb_media_id"] == "m1"
    assert sent["digest"] == ""  # 留空：微信自动取正文前 54 字
    assert sent["need_open_comment"] == 0


def test_add_draft_missing_media_id_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    article = build_draft_article(title="t", content_html="<p>x</p>", thumb_media_id="m1")
    with pytest.raises(WeChatApiError):
        add_draft("tok", article, client=make_client(handler))


def test_network_error_wrapped():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(WeChatApiError) as exc_info:
        fetch_access_token("wx1", "s1", client=make_client(handler))
    assert "不可达" in str(exc_info.value)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
"$PY" -m pytest server/tests/test_wechat_client.py -q
```

Expected: FAIL（ModuleNotFoundError: wechat_client）

- [ ] **Step 3: 实现 wechat_client.py**

```python
"""微信公众号服务端 API 客户端（纯函数）。

约束：不碰 ORM、不读环境变量；所有函数显式收 httpx.Client（测试注入 MockTransport）。
token 的 DB 缓存读写在 runner 侧（见 runner_api.py），本模块只管单次 HTTP 调用。
错误统一抛 WeChatApiError（errcode 非 0 / HTTP >= 400 / 网络错误）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

API_BASE = "https://api.weixin.qq.com"
TOKEN_REFRESH_SKEW_SECONDS = 300  # token 提前 5 分钟视为过期

# 常见 errcode 的中文运维提示
_ERRCODE_HINTS = {
    40164: "请把服务器出口公网 IP 加入公众平台「设置与开发 → 基本配置 → IP 白名单」",
    40001: "AppSecret 无效或已被重置，请在账号管理里更新凭据",
}


class WeChatApiError(Exception):
    """微信接口错误：errcode 非 0、HTTP 错误或网络不可达。"""

    def __init__(
        self,
        message: str,
        errcode: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.errcode = errcode
        self.payload = payload or {}


def _parse_response(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise WeChatApiError(
            f"微信接口返回非 JSON 响应: HTTP {response.status_code}"
        ) from exc
    if response.status_code >= 400:
        raise WeChatApiError(f"微信接口 HTTP 错误: {response.status_code}", payload=payload)
    errcode = payload.get("errcode")
    if errcode not in (None, 0):
        errmsg = payload.get("errmsg", "unknown error")
        hint = _ERRCODE_HINTS.get(errcode)
        message = f"微信接口错误 {errcode}: {errmsg}"
        if hint:
            message = f"{message}（{hint}）"
        raise WeChatApiError(message, errcode=errcode, payload=payload)
    return payload


def _request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise WeChatApiError(f"微信接口不可达: {exc}") from exc
    return _parse_response(response)


def fetch_access_token(app_id: str, app_secret: str, *, client: httpx.Client) -> tuple[str, int]:
    """换取 access_token，返回 (token, expires_in 秒)。"""
    payload = _request(
        client,
        "GET",
        f"{API_BASE}/cgi-bin/token",
        params={"appid": app_id, "secret": app_secret, "grant_type": "client_credential"},
    )
    token = payload.get("access_token")
    expires_in = payload.get("expires_in")
    if not token or not expires_in:
        raise WeChatApiError("token 响应缺少 access_token", payload=payload)
    return token, int(expires_in)


def upload_thumb(access_token: str, filename: str, data: bytes, *, client: httpx.Client) -> str:
    """上传封面缩略图（永久素材，JPG ≤64KB），返回 thumb_media_id。"""
    payload = _request(
        client,
        "POST",
        f"{API_BASE}/cgi-bin/material/add_material",
        params={"access_token": access_token, "type": "thumb"},
        files={"media": (filename, data, "image/jpeg")},
    )
    media_id = payload.get("media_id")
    if not media_id:
        raise WeChatApiError("封面上传未返回 media_id", payload=payload)
    return media_id


def upload_content_image(
    access_token: str, filename: str, data: bytes, *, client: httpx.Client
) -> str:
    """上传正文图（≤1MB JPG/PNG），返回微信图床 URL（外链图会被微信过滤，必须转传）。"""
    mime = "image/png" if filename.lower().endswith(".png") else "image/jpeg"
    payload = _request(
        client,
        "POST",
        f"{API_BASE}/cgi-bin/media/uploadimg",
        params={"access_token": access_token},
        files={"media": (filename, data, mime)},
    )
    url = payload.get("url")
    if not url:
        raise WeChatApiError("正文图上传未返回 url", payload=payload)
    return url


def build_draft_article(*, title: str, content_html: str, thumb_media_id: str) -> dict[str, Any]:
    """构建 draft/add 的单篇 article 结构。

    digest/author 留空（微信自动取正文前 54 字 / 不显示作者）、评论默认关——
    交互稿无配置入口，全自动推导（见 spec 第 6 节）。
    """
    return {
        "article_type": "news",
        "title": title,
        "author": "",
        "digest": "",
        "content": content_html,
        "content_source_url": "",
        "thumb_media_id": thumb_media_id,
        "need_open_comment": 0,
        "only_fans_can_comment": 0,
    }


def add_draft(access_token: str, article: dict[str, Any], *, client: httpx.Client) -> str:
    """新增单图文草稿，返回草稿 media_id。"""
    body = json.dumps({"articles": [article]}, ensure_ascii=False).encode("utf-8")
    payload = _request(
        client,
        "POST",
        f"{API_BASE}/cgi-bin/draft/add",
        params={"access_token": access_token},
        headers={"Content-Type": "application/json; charset=utf-8"},
        content=body,
    )
    media_id = payload.get("media_id")
    if not media_id:
        raise WeChatApiError("草稿创建未返回 media_id", payload=payload)
    return media_id


def make_default_client() -> httpx.Client:
    """生产用默认 client（上传超时放宽到 60s）。调用方负责 close。"""
    return httpx.Client(timeout=httpx.Timeout(20.0, read=60.0, write=60.0))
```

- [ ] **Step 4: 跑测试确认通过**

```bash
"$PY" -m pytest server/tests/test_wechat_client.py -q
```

Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/tasks/drivers/wechat_client.py server/tests/test_wechat_client.py
git commit -m "feat(tasks): 微信公众号 HTTP 客户端——token/素材上传/草稿创建纯函数 + MockTransport 测试"
```

---

### Task 3: 图片压缩纯函数（Pillow）

**Files:**
- Create: `server/app/modules/tasks/drivers/wechat_images.py`
- Test: `server/tests/test_wechat_images.py`

- [ ] **Step 1: 写失败测试**

`server/tests/test_wechat_images.py`：

```python
"""微信图片压缩纯函数测试：Pillow 现场生成测试图，验证 64KB/1MB 边界与格式转换。"""

import io

from PIL import Image

from server.app.modules.tasks.drivers.wechat_images import (
    CONTENT_IMAGE_MAX_BYTES,
    THUMB_MAX_BYTES,
    compress_content_image,
    compress_cover_to_jpeg,
)


def _image_bytes(mode: str, size: tuple[int, int], fmt: str) -> bytes:
    buf = io.BytesIO()
    img = Image.new(mode, size, color=(120, 30, 200) if mode == "RGB" else None)
    # 加噪声让 JPEG 不至于压得过小，测试更真实
    for x in range(0, size[0], 7):
        for y in range(0, size[1], 7):
            img.putpixel((x, y), (x % 256, y % 256, (x * y) % 256) if mode == "RGB" else x % 256)
    img.save(buf, format=fmt)
    return buf.getvalue()


def test_cover_small_jpeg_passthrough_still_jpeg():
    data = _image_bytes("RGB", (200, 150), "JPEG")
    out = compress_cover_to_jpeg(data)
    assert len(out) <= THUMB_MAX_BYTES
    assert Image.open(io.BytesIO(out)).format == "JPEG"


def test_cover_large_png_converted_and_compressed():
    data = _image_bytes("RGB", (2400, 1800), "PNG")
    assert len(data) > THUMB_MAX_BYTES
    out = compress_cover_to_jpeg(data)
    assert len(out) <= THUMB_MAX_BYTES
    assert Image.open(io.BytesIO(out)).format == "JPEG"


def test_cover_rgba_png_flattened():
    buf = io.BytesIO()
    Image.new("RGBA", (800, 600), (255, 0, 0, 128)).save(buf, format="PNG")
    out = compress_cover_to_jpeg(buf.getvalue())
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG"
    assert img.mode == "RGB"


def test_content_image_small_png_kept_as_png():
    data = _image_bytes("RGB", (300, 200), "PNG")
    out, filename = compress_content_image(data, "x.png")
    assert out == data  # 已达标则原样返回
    assert filename.endswith(".png")


def test_content_image_oversize_recompressed_under_1mb():
    data = _image_bytes("RGB", (4000, 3000), "BMP")  # BMP 无压缩，必超 1MB
    assert len(data) > CONTENT_IMAGE_MAX_BYTES
    out, filename = compress_content_image(data, "x.bmp")
    assert len(out) <= CONTENT_IMAGE_MAX_BYTES
    assert filename.endswith(".jpg")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
"$PY" -m pytest server/tests/test_wechat_images.py -q
```

Expected: FAIL（ModuleNotFoundError: wechat_images）

- [ ] **Step 3: 实现 wechat_images.py**

```python
"""微信平台图片规格压缩纯函数。

微信硬约束：封面 thumb 必须 JPG 且 ≤64KB；正文图 JPG/PNG 且 ≤1MB。
策略：先试原图/降质，不够再等比缩边，直到达标；全程纯函数，无 IO。
"""

from __future__ import annotations

import io

from PIL import Image

THUMB_MAX_BYTES = 64 * 1024
CONTENT_IMAGE_MAX_BYTES = 1024 * 1024

_QUALITY_LADDER = (85, 75, 65, 55, 45, 35)
_MIN_EDGE = 64  # 缩边下限，防止死循环


def _flatten_to_rgb(img: Image.Image) -> Image.Image:
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return img.convert("RGB")


def _jpeg_bytes(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _compress_to_jpeg(data: bytes, max_bytes: int) -> bytes:
    """转 RGB JPEG 并迭代降质 + 等比缩边直到 ≤ max_bytes。"""
    img = _flatten_to_rgb(Image.open(io.BytesIO(data)))
    while True:
        for quality in _QUALITY_LADDER:
            out = _jpeg_bytes(img, quality)
            if len(out) <= max_bytes:
                return out
        width, height = img.size
        if min(width, height) <= _MIN_EDGE:
            return out  # 已到缩边下限，返回当前最小结果（极端情况）
        img = img.resize((max(width // 2, _MIN_EDGE), max(height // 2, _MIN_EDGE)))


def compress_cover_to_jpeg(data: bytes) -> bytes:
    """封面：任何输入格式 → RGB JPEG ≤64KB。"""
    return _compress_to_jpeg(data, THUMB_MAX_BYTES)


def compress_content_image(data: bytes, filename: str) -> tuple[bytes, str]:
    """正文图：已是 ≤1MB 的 JPG/PNG 原样返回；否则转 JPEG 压到 ≤1MB。

    返回 (bytes, 上传用文件名)。
    """
    lower = filename.lower()
    if len(data) <= CONTENT_IMAGE_MAX_BYTES and (
        lower.endswith(".jpg") or lower.endswith(".jpeg") or lower.endswith(".png")
    ):
        return data, filename
    return _compress_to_jpeg(data, CONTENT_IMAGE_MAX_BYTES), "image.jpg"
```

- [ ] **Step 4: 跑测试确认通过**

```bash
"$PY" -m pytest server/tests/test_wechat_images.py -q
```

Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/tasks/drivers/wechat_images.py server/tests/test_wechat_images.py
git commit -m "feat(tasks): 微信图片规格压缩纯函数——封面JPG≤64KB/正文图≤1MB"
```

---

### Task 4: 账号 API——创建 / 更新 / 凭据掩码 / verify-credentials / 浏览器流守卫

**Files:**
- Modify: `server/app/modules/accounts/schemas.py`（请求体类）
- Modify: `server/app/modules/accounts/service.py`（create_api_account / update_account_fields / token 缓存助手）
- Modify: `server/app/modules/accounts/router.py`（POST ""、扩 PATCH、POST verify-credentials、login 流守卫）
- Modify: `server/app/modules/tasks/drivers/__init__.py`（`is_api_driver` 助手）
- Test: `server/tests/test_accounts_api_wechat.py`（新文件）

前置：Task 1（模型字段）、Task 2（`fetch_access_token` / `WeChatApiError` 签名；测试中 monkeypatch，不打真网络）。

- [ ] **Step 1: 写失败测试**

`server/tests/test_accounts_api_wechat.py`：

```python
"""微信公众号账号 API 测试：创建校验、secret 掩码、verify-credentials、PATCH、浏览器流守卫。"""

import pytest

from server.app.modules.tasks.drivers.wechat_client import WeChatApiError
from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


def _ensure_wechat_platform(test_app) -> None:
    from server.app.modules.system.models import Platform

    with test_app.session_factory() as db:
        if (
            db.query(Platform).filter(Platform.code == "wechat_mp").first()
            is None
        ):
            db.add(Platform(code="wechat_mp", name="微信公众号"))
            db.commit()


def _create_payload(**overrides):
    payload = {
        "platform_code": "wechat_mp",
        "display_name": "测试公众号",
        "api_credentials": {"app_id": "wx8f2a91c0d3e5b6", "app_secret": "secret-end-3a7f"},
        "contact": "186***3027",
        "note": "主力号",
    }
    payload.update(overrides)
    return payload


def test_create_wechat_account_masks_secret(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        resp = test_app.client.post("/api/accounts", json=_create_payload())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["platform_code"] == "wechat_mp"
        assert body["app_id"] == "wx8f2a91c0d3e5b6"
        assert body["app_secret_tail"] == "3a7f"
        assert body["state_path"] is None
        assert body["distribution_enabled"] is True
        assert body["platform_user_id"] == "wx8f2a91c0d3e5b6"  # AppID 即平台侧标识
        assert "api_credentials" not in body  # 原文永不回传
    finally:
        test_app.cleanup()


def test_create_duplicate_app_id_conflict(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        assert test_app.client.post("/api/accounts", json=_create_payload()).status_code == 200
        resp = test_app.client.post("/api/accounts", json=_create_payload())
        assert resp.status_code == 409
    finally:
        test_app.cleanup()


def test_create_browser_platform_rejected(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        resp = test_app.client.post(
            "/api/accounts", json=_create_payload(platform_code="toutiao")
        )
        assert resp.status_code == 400  # 浏览器平台走 login 流，不走凭据创建
    finally:
        test_app.cleanup()


def test_verify_credentials_success_sets_valid_and_caches_token(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        account_id = test_app.client.post("/api/accounts", json=_create_payload()).json()["id"]

        monkeypatch.setattr(
            "server.app.modules.accounts.service.wechat_fetch_access_token",
            lambda app_id, app_secret, client=None: ("tok-1", 7200),
        )
        resp = test_app.client.post(f"/api/accounts/{account_id}/verify-credentials")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "valid"

        from server.app.modules.accounts.models import Account

        with test_app.session_factory() as db:
            cache = db.get(Account, account_id).api_token_cache
        assert cache["access_token"] == "tok-1"
        assert cache["expires_at"] > 0
    finally:
        test_app.cleanup()


def test_verify_credentials_failure_sets_expired_and_returns_hint(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        account_id = test_app.client.post("/api/accounts", json=_create_payload()).json()["id"]

        def boom(app_id, app_secret, client=None):
            raise WeChatApiError("微信接口错误 40164: invalid ip（请把服务器出口公网 IP 加入…）", errcode=40164)

        monkeypatch.setattr(
            "server.app.modules.accounts.service.wechat_fetch_access_token", boom
        )
        resp = test_app.client.post(f"/api/accounts/{account_id}/verify-credentials")
        assert resp.status_code == 400
        assert "40164" in resp.json()["detail"]

        from server.app.modules.accounts.models import Account

        with test_app.session_factory() as db:
            assert db.get(Account, account_id).status == "expired"
    finally:
        test_app.cleanup()


def test_patch_updates_fields_and_replaces_secret(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        account_id = test_app.client.post("/api/accounts", json=_create_payload()).json()["id"]

        resp = test_app.client.patch(
            f"/api/accounts/{account_id}",
            json={
                "display_name": "云栖",
                "distribution_enabled": False,
                "api_credentials": {"app_id": "wx8f2a91c0d3e5b6", "app_secret": "new-secret-9b2c"},
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["display_name"] == "云栖"
        assert body["distribution_enabled"] is False
        assert body["app_secret_tail"] == "9b2c"
    finally:
        test_app.cleanup()


def test_patch_rename_only_still_works(monkeypatch):
    """旧前端只发 display_name 的改名调用保持兼容。"""
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        account_id = test_app.client.post("/api/accounts", json=_create_payload()).json()["id"]
        resp = test_app.client.patch(
            f"/api/accounts/{account_id}", json={"display_name": "改名"}
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "改名"
        assert resp.json()["app_secret_tail"] == "3a7f"  # 凭据未被碰
    finally:
        test_app.cleanup()


def test_login_session_rejected_for_api_platform(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        account_id = test_app.client.post("/api/accounts", json=_create_payload()).json()["id"]
        resp = test_app.client.post(f"/api/accounts/{account_id}/login-session")
        assert resp.status_code == 400
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
"$PY" -m pytest server/tests/test_accounts_api_wechat.py -q
```

Expected: FAIL（POST /api/accounts 405、verify-credentials 404 等）

- [ ] **Step 3: drivers/__init__.py 加 is_api_driver 助手**

在 `server/app/modules/tasks/drivers/__init__.py` 末尾追加：

```python
def is_api_driver(platform_code: str) -> bool:
    """该平台的默认驱动是否为 API 型（mode='api'，发布不走浏览器）。未注册平台返回 False。"""
    driver = _REGISTRY.get(platform_code)
    return getattr(driver, "mode", "browser") == "api"
```

> Task 6 才注册 wechat_mp 驱动。本 task 的 router 守卫不依赖驱动注册：API 平台判定用
> 模块级常量兜底（见 Step 5 的 `_API_PLATFORM_CODES`），`is_api_driver` 供 executor 用。

- [ ] **Step 4: schemas.py 加请求体**

`server/app/modules/accounts/schemas.py` 追加（`AccountRenameRequest` 删除，引用处换 `AccountUpdateRequest`）：

```python
class ApiCredentialsIn(BaseModel):
    app_id: str = Field(min_length=1, max_length=100)
    app_secret: str = Field(min_length=1, max_length=200)


class ApiAccountCreate(BaseModel):
    """API 型平台（如微信公众号）账号创建：凭据直填，无浏览器登录。"""

    platform_code: str = Field(min_length=1, max_length=50)
    display_name: str = Field(min_length=1, max_length=200)
    api_credentials: ApiCredentialsIn
    contact: str | None = Field(default=None, max_length=200)
    note: str | None = None
    avatar_asset_id: str | None = Field(default=None, max_length=64)
    distribution_enabled: bool = True


class AccountUpdateRequest(BaseModel):
    """账号通用 PATCH：全部可选，未传字段不动（None 同样视为未传，沿用项目 PATCH 语义）。"""

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    contact: str | None = Field(default=None, max_length=200)
    note: str | None = None
    avatar_asset_id: str | None = Field(default=None, max_length=64)
    distribution_enabled: bool | None = None
    api_credentials: ApiCredentialsIn | None = None  # 传则整体替换（含 secret）
```

- [ ] **Step 5: service.py 加创建 / 更新 / token 缓存函数**

`server/app/modules/accounts/service.py` 追加（import 区加
`from server.app.modules.tasks.drivers.wechat_client import fetch_access_token as wechat_fetch_access_token, make_default_client, TOKEN_REFRESH_SKEW_SECONDS, WeChatApiError`
以及 `import time`、`from server.app.shared.errors import ConflictError, ValidationError`，按文件现有 import 风格归位）：

```python
# API 型平台代码集合：驱动注册前 router 守卫的兜底判定（驱动注册后以 is_api_driver 为准）
_API_PLATFORM_CODES = {"wechat_mp"}


def is_api_platform_code(code: str) -> bool:
    from server.app.modules.tasks.drivers import is_api_driver

    return code in _API_PLATFORM_CODES or is_api_driver(code)


def create_api_account(db: Session, user_id: int, payload: "ApiAccountCreate") -> Account:
    """创建 API 型平台账号：凭据直存，platform_user_id 取 AppID（吃唯一约束防重复登记）。"""
    if not is_api_platform_code(payload.platform_code):
        raise ValidationError(f"平台 {payload.platform_code} 为浏览器登录接入，请走扫码授权流程")
    platform = db.execute(
        select(Platform).where(Platform.code == payload.platform_code)
    ).scalar_one_or_none()
    if platform is None:
        raise ValidationError(f"平台不存在: {payload.platform_code}")

    app_id = payload.api_credentials.app_id
    duplicate = db.execute(
        select(Account).where(
            Account.user_id == user_id,
            Account.platform_id == platform.id,
            Account.platform_user_id == app_id,
            Account.is_deleted == False,  # noqa: E712
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ConflictError(f"该 AppID 已登记: {app_id}")

    account = Account(
        user_id=user_id,
        platform=platform,
        display_name=payload.display_name,
        platform_user_id=app_id,
        status="unknown",  # verify-credentials 通过后置 valid
        state_path=None,
        api_credentials={"app_id": app_id, "app_secret": payload.api_credentials.app_secret},
        contact=payload.contact,
        note=payload.note,
        avatar_asset_id=payload.avatar_asset_id,
        distribution_enabled=payload.distribution_enabled,
    )
    db.add(account)
    db.flush()
    return account


def update_account_fields(db: Session, account: Account, payload: "AccountUpdateRequest") -> Account:
    """通用 PATCH：仅更新显式传入的字段；api_credentials 整体替换。"""
    data = payload.model_dump(exclude_unset=True)
    if data.get("display_name") is not None:
        account.display_name = data["display_name"]
    if data.get("contact") is not None:
        account.contact = data["contact"]
    if data.get("note") is not None:
        account.note = data["note"]
    if data.get("avatar_asset_id") is not None:
        account.avatar_asset_id = data["avatar_asset_id"]
    if data.get("distribution_enabled") is not None:
        account.distribution_enabled = data["distribution_enabled"]
    if data.get("api_credentials") is not None:
        creds = payload.api_credentials
        account.api_credentials = {"app_id": creds.app_id, "app_secret": creds.app_secret}
        account.platform_user_id = creds.app_id
        account.api_token_cache = None  # 换凭据后旧 token 作废
    account.updated_at = utcnow()
    db.flush()
    return account


def verify_api_credentials(db: Session, account: Account) -> Account:
    """实调微信 token 接口验证凭据（强制刷新）。成功 → valid + 缓存 token；失败 → expired + 透传错误。"""
    creds = account.api_credentials or {}
    if not creds.get("app_id") or not creds.get("app_secret"):
        raise ValidationError("账号未配置 AppID/AppSecret")
    client = make_default_client()
    try:
        token, expires_in = wechat_fetch_access_token(
            creds["app_id"], creds["app_secret"], client=client
        )
    except WeChatApiError as exc:
        account.status = "expired"
        account.last_checked_at = utcnow()
        db.flush()
        raise ValidationError(str(exc)) from exc
    finally:
        client.close()
    # 整体赋新 dict（不要原地改），SQLAlchemy 才能检测到 JSON 列变更
    account.api_token_cache = {
        "access_token": token,
        "expires_at": int(time.time()) + expires_in,
    }
    account.status = "valid"
    account.last_checked_at = utcnow()
    db.flush()
    return account


def get_cached_wechat_token(account: Account) -> str | None:
    """读 DB token 缓存；不存在或剩余有效期不足 5 分钟返回 None。"""
    cache = account.api_token_cache or {}
    token = cache.get("access_token")
    expires_at = int(cache.get("expires_at") or 0)
    if not token or expires_at <= int(time.time()) + TOKEN_REFRESH_SKEW_SECONDS:
        return None
    return token
```

> `select`、`Platform`、`utcnow` 等若 service.py 已有 import 则复用。
> monkeypatch 点位约定：测试以 `server.app.modules.accounts.service.wechat_fetch_access_token`
> 打桩，因此 import 必须用 `from ... import fetch_access_token as wechat_fetch_access_token` 形式。

- [ ] **Step 6: router.py 加端点 + 守卫**

`server/app/modules/accounts/router.py`：

import 区追加 `from server.app.core.limiter import limiter` 与新 schema / service 函数。

新增创建端点（放在 `read_accounts` 之后）：

```python
@router.post("", response_model=AccountRead)
def create_api_account_endpoint(
    payload: ApiAccountCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AccountRead:
    """创建 API 型平台账号（凭据直填）。浏览器平台返回 400 走扫码授权流。"""
    account = service.create_api_account(db, current_user.id, payload)
    db.commit()
    return to_account_read(service.get_account(db, account.id) or account)
```

新增验证端点：

```python
@router.post("/{account_id:int}/verify-credentials", response_model=AccountRead)
@limiter.limit("10/minute")
def verify_account_credentials_endpoint(
    account_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AccountRead:
    """实调平台接口验证凭据（「前往授权」第 2 步）。失败时账号置 expired 并透传平台错误。"""
    account = _verify_account_ownership(service.get_account(db, account_id), current_user)
    if not service.is_api_platform_code(account.platform.code):
        raise HTTPException(status_code=400, detail="该平台为浏览器登录接入，无凭据可验证")
    try:
        account = service.verify_api_credentials(db, account)
        db.commit()
    except Exception:
        db.commit()  # 失败分支也要落 status=expired
        raise
    return to_account_read(account)
```

PATCH 端点改造（替换原 `rename_existing_account` 函数体，函数与路由保留）：

```python
@router.patch("/{account_id:int}", response_model=AccountRead)
def update_existing_account(
    account_id: int,
    payload: AccountUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AccountRead:
    account = _verify_account_ownership(service.get_account(db, account_id), current_user)
    account = service.update_account_fields(db, account, payload)
    db.commit()
    return to_account_read(account)
```

浏览器流守卫——在 `login_platform_account`、`start_platform_login_session_endpoint`、
`start_existing_account_login_session_endpoint`、`check_existing_account`、
`relogin_existing_account` 五个端点的入口（取得 platform_code / account 之后）各加：

```python
    if service.is_api_platform_code(platform_code):  # 账号级端点用 account.platform.code
        raise HTTPException(status_code=400, detail="该平台为 API 接入，无需浏览器登录")
```

- [ ] **Step 7: 跑测试确认通过 + 回归**

```bash
"$PY" -m pytest server/tests/test_accounts_api_wechat.py server/tests/test_accounts_api.py -q
```

Expected: 全部 PASS（旧 rename 用例若引用已删的 `AccountRenameRequest`，改为 `AccountUpdateRequest`）。

- [ ] **Step 8: Commit**

```bash
git add server/app/modules/accounts/ server/app/modules/tasks/drivers/__init__.py server/tests/test_accounts_api_wechat.py
git commit -m "feat(accounts): API 型账号创建/PATCH/凭据验证端点——secret 掩码回传 + 浏览器流守卫"
```

---

### Task 5: pipeline distribute 自动派号过滤分发开关

**Files:**
- Modify: `server/app/modules/pipelines/nodes/distribute_node.py:15-23`
- Test: `server/tests/test_auto_distribute.py`（追加用例）

前置：Task 1。

- [ ] **Step 1: 写失败测试**

在 `server/tests/test_auto_distribute.py` 追加（参考该文件既有 fixture 风格创建账号/文章；下面给出独立可运行的骨架，执行者按文件内既有 helper 改写创建语句）：

```python
def test_distribute_skips_disabled_accounts(monkeypatch):
    """distribution_enabled=False 的账号被自动派号过滤；全部停用时安静跳过不报错。"""
    test_app = build_test_app(monkeypatch)
    try:
        # 按本文件既有 helper 建：平台 + 账号 a1(启用) + a2(停用) + 已审核文章 art
        # a2 = ...; a2.distribution_enabled = False; db.commit()
        from server.app.modules.pipelines.nodes.base import NodeRunContext
        from server.app.modules.pipelines.nodes.distribute_node import run_distribute

        ctx = NodeRunContext(
            config={"account_ids": [a1.id, a2.id]},
            inputs={"article_ids": [art.id]},
            user_id=admin_id,
            session_factory=test_app.session_factory,
        )
        result = run_distribute(ctx)
        # 任务创建成功，且只派给了启用账号 a1
        with test_app.session_factory() as db:
            from server.app.modules.tasks.models import PublishRecord

            records = db.query(PublishRecord).all()
            assert {r.account_id for r in records} == {a1.id}

        # 全部停用 → 安静跳过
        ctx_all_disabled = NodeRunContext(
            config={"account_ids": [a2.id]},
            inputs={"article_ids": [art.id]},
            user_id=admin_id,
            session_factory=test_app.session_factory,
        )
        result2 = run_distribute(ctx_all_disabled)
        assert "skipped" in result2.output
    finally:
        test_app.cleanup()
```

> `NodeRunContext` 的构造参数以 `nodes/base.py` 实际定义为准（执行者先读该文件），
> 上面是意图示例；既有 `test_auto_distribute.py` 里已有同构用例可抄。

- [ ] **Step 2: 跑测试确认失败**

```bash
"$PY" -m pytest server/tests/test_auto_distribute.py -q -k disabled
```

Expected: FAIL（停用账号也被派了号 / 全停用时抛 ValidationError）

- [ ] **Step 3: 实现过滤**

`server/app/modules/pipelines/nodes/distribute_node.py`，`run_distribute` 开头
`account_ids` 取得之后、构建 `accounts` 之前插入（同时把函数后部已有的 `db = ctx.session_factory()` 块合并复用这里打开的 db；保持单次开闭）：

```python
    db = ctx.session_factory()
    try:
        from server.app.modules.accounts.models import Account

        enabled_ids = {
            row[0]
            for row in db.query(Account.id)
            .filter(Account.id.in_(account_ids), Account.distribution_enabled == True)  # noqa: E712
            .all()
        }
        # 保持配置顺序，过滤停用账号；全停用 → 安静跳过（对齐空 article_ids 语义）
        active_account_ids = [a for a in account_ids if a in enabled_ids]
        if not active_account_ids:
            return NodeResult(output={"skipped": "无启用分发的账号"}, article_ids=[])
        accounts = [
            TaskAccountInput(account_id=a, sort_order=i)
            for i, a in enumerate(active_account_ids)
        ]
        ...  # 原有 article_ids / group_id 分支与 create_task 调用挪进此 try 块
    finally:
        db.close()
```

- [ ] **Step 4: 跑测试确认通过 + 该文件回归**

```bash
"$PY" -m pytest server/tests/test_auto_distribute.py -q
```

Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/pipelines/nodes/distribute_node.py server/tests/test_auto_distribute.py
git commit -m "feat(pipelines): distribute 自动派号过滤 distribution_enabled=false 账号，全停用安静跳过"
```

---

### Task 6: ApiPublishPayload + 微信驱动 + runner_api + executor 分叉 + 注册

**Files:**
- Modify: `server/app/modules/tasks/drivers/base.py`（加 ApiPublishPayload）
- Create: `server/app/modules/tasks/drivers/wechat_mp.py`
- Create: `server/app/modules/tasks/runner_api.py`
- Modify: `server/app/modules/tasks/executor.py:871-893`（build_publish_runner_for_record 分叉）
- Modify: `server/app/main.py`（驱动 import 注册行）
- Test: `server/tests/test_wechat_publish.py`

前置：Task 1、2、3。

- [ ] **Step 1: base.py 加 ApiPublishPayload**

`server/app/modules/tasks/drivers/base.py` 追加：

```python
@dataclass(frozen=True)
class ApiPublishPayload:
    """API 型平台驱动的发布载荷：纯数据，含已就绪的 access_token，不含 secret。

    与 PublishPayload 的区别：无 state_path/account_key（无浏览器态）；cover_path 可空
    （驱动内回落正文首图）；token 由 runner_api 从 DB 缓存解析后注入。
    """

    title: str
    body_segments: list[BodySegment]
    cover_path: Path | None
    display_name: str
    platform_code: str
    access_token: str
    temp_files: tuple[Path, ...] = ()
```

- [ ] **Step 2: 写失败测试**

`server/tests/test_wechat_publish.py`：

```python
"""微信驱动 publish_api 测试：MockTransport 全打桩，验证封面回落/转传/HTML 重组/错误映射。

无 DB 用例（驱动纯函数）+ 1 个 mysql 用例（executor 分叉走 API 路径）。
"""

import io
from pathlib import Path

import httpx
import pytest
from PIL import Image

from server.app.modules.articles.parser import BodySegment
from server.app.modules.tasks.drivers.base import ApiPublishPayload, PublishError
from server.app.modules.tasks.drivers.wechat_mp import WeChatMpDriver


def _jpeg_file(tmp_path: Path, name: str, size=(400, 300)) -> Path:
    p = tmp_path / name
    Image.new("RGB", size, (10, 20, 30)).save(p, format="JPEG")
    return p


def _payload(tmp_path: Path, *, cover: Path | None, segments: list[BodySegment]):
    return ApiPublishPayload(
        title="测试标题",
        body_segments=segments,
        cover_path=cover,
        display_name="测试公众号",
        platform_code="wechat_mp",
        access_token="tok",
    )


def _mock_client(uploads: list[str]):
    """打桩三类请求：thumb 上传 → m1；uploadimg → 递增 URL；draft/add → draft-1。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/cgi-bin/material/add_material":
            uploads.append("thumb")
            return httpx.Response(200, json={"media_id": "m1"})
        if path == "/cgi-bin/media/uploadimg":
            uploads.append("img")
            return httpx.Response(
                200, json={"url": f"https://mmbiz.qpic.cn/{len(uploads)}.jpg"}
            )
        if path == "/cgi-bin/draft/add":
            uploads.append("draft")
            return httpx.Response(200, json={"media_id": "draft-1"})
        raise AssertionError(f"unexpected path {path}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_publish_api_full_flow(tmp_path):
    cover = _jpeg_file(tmp_path, "cover.jpg")
    body_img = _jpeg_file(tmp_path, "body.jpg")
    segments = [
        BodySegment(kind="text", text="开头", heading_level=None),
        BodySegment(kind="image", image_asset_id="a1", image_path=body_img),
        BodySegment(kind="text", text="小标题", heading_level=2),
    ]
    uploads: list[str] = []
    driver = WeChatMpDriver()
    result = driver.publish_api(
        payload=_payload(tmp_path, cover=cover, segments=segments),
        client=_mock_client(uploads),
    )
    assert result.url is None
    assert "draft-1" in result.message
    assert uploads == ["thumb", "img", "draft"]


def test_publish_api_cover_fallback_to_first_body_image(tmp_path):
    body_img = _jpeg_file(tmp_path, "body.jpg")
    segments = [BodySegment(kind="image", image_asset_id="a1", image_path=body_img)]
    uploads: list[str] = []
    driver = WeChatMpDriver()
    result = driver.publish_api(
        payload=_payload(tmp_path, cover=None, segments=segments),
        client=_mock_client(uploads),
    )
    assert "draft-1" in result.message
    assert "thumb" in uploads  # 正文首图被用作封面上传


def test_publish_api_no_image_at_all_raises(tmp_path):
    segments = [BodySegment(kind="text", text="只有文字")]
    driver = WeChatMpDriver()
    with pytest.raises(PublishError, match="封面"):
        driver.publish_api(
            payload=_payload(tmp_path, cover=None, segments=segments),
            client=_mock_client([]),
        )


def test_publish_api_wechat_error_mapped_to_publish_error(tmp_path):
    cover = _jpeg_file(tmp_path, "cover.jpg")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 45009, "errmsg": "api freq out of limit"})

    driver = WeChatMpDriver()
    with pytest.raises(PublishError, match="45009"):
        driver.publish_api(
            payload=_payload(
                tmp_path, cover=cover, segments=[BodySegment(kind="text", text="x")]
            ),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )


def test_segments_to_html_headings_and_bold():
    from server.app.modules.tasks.drivers.wechat_mp import segments_to_html

    segments = [
        BodySegment(kind="text", text="大标题", heading_level=1),
        BodySegment(kind="text", text="加粗", bold=True),
        BodySegment(kind="text", text="普通段落"),
        BodySegment(kind="image", image_asset_id="a1"),
    ]
    html = segments_to_html(segments, {3: "https://mmbiz.qpic.cn/1.jpg"})
    assert "<h1>大标题</h1>" in html
    assert "<p><strong>加粗</strong></p>" in html
    assert "<p>普通段落</p>" in html
    assert '<img src="https://mmbiz.qpic.cn/1.jpg"' in html


def test_segments_to_html_escapes_text():
    from server.app.modules.tasks.drivers.wechat_mp import segments_to_html

    html = segments_to_html([BodySegment(kind="text", text="a<b>&c")], {})
    assert "a&lt;b&gt;&amp;c" in html
```

- [ ] **Step 3: 跑测试确认失败**

```bash
"$PY" -m pytest server/tests/test_wechat_publish.py -q
```

Expected: FAIL（ModuleNotFoundError: wechat_mp）

- [ ] **Step 4: 实现 wechat_mp.py 驱动**

```python
"""微信公众号 API 驱动：草稿箱单图文发布（mode='api'，无浏览器）。

链路：封面（无则回落正文首图）压 JPG≤64KB 传 thumb → 正文图逐张压 ≤1MB 转传换
微信 URL → body_segments 重组 HTML → draft/add。终点即草稿箱（spec：不调 freepublish）。
驱动纯数据进出：凭据/token 由 runner_api 解析后经 payload 注入，不碰 ORM。
"""

from __future__ import annotations

import html as html_lib

import httpx

from server.app.modules.articles.parser import BodySegment
from server.app.modules.tasks.drivers import register
from server.app.modules.tasks.drivers.base import (
    ApiPublishPayload,
    PublishError,
    PublishResult,
)
from server.app.modules.tasks.drivers.wechat_client import (
    WeChatApiError,
    add_draft,
    build_draft_article,
    make_default_client,
    upload_content_image,
    upload_thumb,
)
from server.app.modules.tasks.drivers.wechat_images import (
    compress_content_image,
    compress_cover_to_jpeg,
)


def segments_to_html(segments: list[BodySegment], image_urls: dict[int, str]) -> str:
    """body_segments → 微信草稿 HTML。image_urls 按 segment 下标映射微信图床 URL。"""
    parts: list[str] = []
    for index, seg in enumerate(segments):
        if seg.kind == "image":
            url = image_urls.get(index)
            if url:
                parts.append(f'<p><img src="{url}" style="max-width:100%;"></p>')
            continue
        text = html_lib.escape(seg.text).replace("\n", "<br>")
        if not text.strip():
            continue
        if seg.heading_level == 1:
            parts.append(f"<h1>{text}</h1>")
        elif seg.heading_level == 2:
            parts.append(f"<h2>{text}</h2>")
        elif seg.bold:
            parts.append(f"<p><strong>{text}</strong></p>")
        else:
            parts.append(f"<p>{text}</p>")
    return "".join(parts)


class WeChatMpDriver:
    code = "wechat_mp"
    name = "微信公众号"
    home_url = "https://mp.weixin.qq.com"
    publish_url = "https://mp.weixin.qq.com"
    mode = "api"  # build_publish_runner_for_record 据此走 runner_api 路径

    def detect_logged_in(self, *, url: str, title: str, body: str) -> bool:
        return False  # API 平台不走浏览器登录检测

    def publish(self, *, page, context, payload, stop_before_publish):  # pragma: no cover
        raise PublishError("微信公众号为 API 接入，不支持浏览器发布路径")

    def publish_api(
        self, *, payload: ApiPublishPayload, client: httpx.Client | None = None
    ) -> PublishResult:
        owns_client = client is None
        if client is None:
            client = make_default_client()
        try:
            return self._publish_api(payload=payload, client=client)
        except WeChatApiError as exc:
            raise PublishError(str(exc)) from exc
        finally:
            if owns_client:
                client.close()

    def _publish_api(self, *, payload: ApiPublishPayload, client: httpx.Client) -> PublishResult:
        token = payload.access_token

        cover_path = payload.cover_path
        if cover_path is None:
            cover_path = next(
                (s.image_path for s in payload.body_segments if s.kind == "image" and s.image_path),
                None,
            )
        if cover_path is None:
            raise PublishError("公众号草稿需要封面图（或正文至少一张图）")
        thumb_media_id = upload_thumb(
            token, "cover.jpg", compress_cover_to_jpeg(cover_path.read_bytes()), client=client
        )

        image_urls: dict[int, str] = {}
        for index, seg in enumerate(payload.body_segments):
            if seg.kind != "image" or seg.image_path is None:
                continue
            data, filename = compress_content_image(
                seg.image_path.read_bytes(), seg.image_path.name
            )
            image_urls[index] = upload_content_image(token, filename, data, client=client)

        content_html = segments_to_html(payload.body_segments, image_urls)
        if not content_html:
            raise PublishError("正文为空，无法创建公众号草稿")
        article = build_draft_article(
            title=payload.title, content_html=content_html, thumb_media_id=thumb_media_id
        )
        media_id = add_draft(token, article, client=client)
        return PublishResult(
            url=None,
            title=payload.title,
            message=f"草稿已写入公众号草稿箱 media_id={media_id}",
        )


register(WeChatMpDriver())
```

- [ ] **Step 5: 实现 runner_api.py**

```python
"""API 型平台发布入口：不起浏览器，token 的 DB 读写在这里完成（驱动不碰 ORM）。

与 runner.run_publish 的关系：build_publish_runner_for_record 按驱动 mode 分叉，
API 驱动进本模块。资产解析复用 runner 的 stock image 拉取与临时文件清理。
"""

from __future__ import annotations

import time
from pathlib import Path

from server.app.modules.accounts.models import Account
from server.app.modules.articles.models import Article
from server.app.modules.articles.parser import BodySegment, parse_body_segments
from server.app.modules.articles.store import resolve_asset_path
from server.app.modules.tasks.drivers.base import ApiPublishPayload, PublishError, PublishResult
from server.app.modules.tasks.drivers.wechat_client import (
    fetch_access_token,
    make_default_client,
)
from server.app.shared.diagnostics import publish_step


def _resolve_access_token(account_id: int) -> str:
    """读 DB token 缓存，过期则刷新并写回。自开 session（发布线程内，不复用外部 session）。"""
    from server.app.db.session import SessionLocal
    from server.app.modules.accounts.service import get_cached_wechat_token

    db = SessionLocal()
    try:
        account = db.get(Account, account_id)
        if account is None:
            raise PublishError(f"账号不存在: {account_id}")
        token = get_cached_wechat_token(account)
        if token:
            return token
        creds = account.api_credentials or {}
        if not creds.get("app_id") or not creds.get("app_secret"):
            raise PublishError("账号未配置 AppID/AppSecret，请先在媒体矩阵完成授权")
        client = make_default_client()
        try:
            token, expires_in = fetch_access_token(
                creds["app_id"], creds["app_secret"], client=client
            )
        finally:
            client.close()
        # 整体赋新 dict，SQLAlchemy 才能检测 JSON 变更
        account.api_token_cache = {
            "access_token": token,
            "expires_at": int(time.time()) + expires_in,
        }
        db.commit()
        return token
    finally:
        db.close()


def _build_api_payload(article: Article, account: Account, access_token: str) -> ApiPublishPayload:
    """解析正文段与资产路径（含图片库临时文件）。封面可空——驱动内回落正文首图。"""
    from server.app.modules.tasks.runner import (
        _cleanup_temp_files,
        _resolve_stock_image_path,
    )

    cover_path: Path | None = None
    if article.cover_asset is not None:
        cover_path = resolve_asset_path(article.cover_asset)

    raw_segments = parse_body_segments(article)
    resolved: list[BodySegment] = []
    temp_files: list[Path] = []
    try:
        for seg in raw_segments:
            if seg.kind == "image" and seg.image_asset_id:
                asset_link = next(
                    (
                        link
                        for link in article.body_assets
                        if link.asset_id == seg.image_asset_id and link.asset is not None
                    ),
                    None,
                )
                if asset_link is None:
                    raise PublishError(f"正文图片资源不存在或未加载: {seg.image_asset_id}")
                resolved.append(
                    BodySegment(
                        kind="image",
                        image_asset_id=seg.image_asset_id,
                        image_path=resolve_asset_path(asset_link.asset),
                    )
                )
            elif seg.kind == "image" and seg.stock_image_id is not None:
                image_path = _resolve_stock_image_path(seg.stock_image_id)
                temp_files.append(image_path)
                resolved.append(
                    BodySegment(
                        kind="image", stock_image_id=seg.stock_image_id, image_path=image_path
                    )
                )
            else:
                resolved.append(seg)
        return ApiPublishPayload(
            title=article.title,
            body_segments=resolved,
            cover_path=cover_path,
            display_name=account.display_name,
            platform_code=account.platform.code,
            access_token=access_token,
            temp_files=tuple(temp_files),
        )
    except Exception:
        _cleanup_temp_files(temp_files)
        raise


def run_publish_api(*, article: Article, account: Account, driver) -> PublishResult:
    """API 平台发布：token 解析 → payload 构建 → driver.publish_api。

    stop_before_publish 对草稿箱终点是 no-op（草稿箱本身就是「停在发布前」），故无此参数。
    """
    from server.app.modules.tasks.runner import _cleanup_temp_files

    if not article.title or not article.title.strip():
        raise PublishError("标题不能为空")

    with publish_step("resolve api access token"):
        access_token = _resolve_access_token(account.id)
    payload = _build_api_payload(article, account, access_token)
    try:
        with publish_step("api driver publish flow"):
            return driver.publish_api(payload=payload)
    finally:
        _cleanup_temp_files(payload.temp_files)
```

- [ ] **Step 6: executor 分叉 + main.py 注册**

`server/app/modules/tasks/executor.py` — `build_publish_runner_for_record` 改为：

```python
def build_publish_runner_for_record(record: PublishRecord):
    """构造该记录的发布闭包：预绑 record_id + 浏览器 channel/可执行路径，返回 (article, account) → PublishResult。

    API 型平台（驱动 mode='api'，如公众号）分叉到 runner_api（无浏览器）；
    浏览器平台驱动选择仍在 runner.run_publish 内按账号 state_path 的 platform_code 决定。
    懒导入 runner 避免循环依赖。
    """
    from server.app.modules.tasks.drivers import is_api_driver, resolve_driver

    platform_code = record.platform.code if record.platform is not None else None
    if platform_code and is_api_driver(platform_code):
        from server.app.modules.tasks.runner_api import run_publish_api

        driver = resolve_driver(platform_code)

        def _api_runner(article, account, *, stop_before_publish=False):
            # 草稿箱终点：stop_before_publish 是 no-op，接受参数仅为闭包签名兼容
            return run_publish_api(article=article, account=account, driver=driver)

        return _api_runner

    from server.app.modules.tasks.runner import run_publish

    settings = get_settings()
    channel = settings.publish_browser_channel
    executable_path = settings.publish_browser_executable_path
    _record_id = record.id

    def _runner(article, account, *, stop_before_publish=False):
        return run_publish(
            record_id=_record_id,
            article=article,
            account=account,
            channel=channel,
            executable_path=executable_path,
            stop_before_publish=stop_before_publish,
        )

    return _runner
```

`server/app/main.py` — 在既有 `import server.app.modules.tasks.drivers.toutiao*` 行旁追加：

```python
import server.app.modules.tasks.drivers.wechat_mp  # noqa: F401
```

- [ ] **Step 7: 跑测试确认通过 + 任务执行回归**

```bash
"$PY" -m pytest server/tests/test_wechat_publish.py -q
"$PY" -m pytest server/tests/test_tasks_api.py -q
```

Expected: 全部 PASS（浏览器路径零行为变化——分叉只对 mode='api' 生效）。

- [ ] **Step 8: Commit**

```bash
git add server/app/modules/tasks/drivers/base.py server/app/modules/tasks/drivers/wechat_mp.py server/app/modules/tasks/runner_api.py server/app/modules/tasks/executor.py server/app/main.py server/tests/test_wechat_publish.py
git commit -m "feat(tasks): 微信公众号 API 驱动 + runner_api 无浏览器发布路径——executor 按驱动 mode 分叉"
```

---

### Task 7: 文档 + 全量回归

**Files:**
- Modify: `CLAUDE.md`（PlatformDriver 节加 API 驱动说明；Domain Modules accounts 节加凭据字段；Gotchas 加 IP 白名单）

- [ ] **Step 1: CLAUDE.md 增量**

PlatformDriver 节末尾追加：

```markdown
驱动分两类：浏览器驱动（默认，实现 `publish(page, context, ...)`）和 **API 驱动**
（类属性 `mode = "api"`，实现 `publish_api(payload: ApiPublishPayload)`，发布不起浏览器，
`build_publish_runner_for_record` 据此分叉到 `runner_api.run_publish_api`）。
微信公众号（`wechat_mp`）是首个 API 驱动：终点为草稿箱（draft/add 即 succeeded，不调
freepublish），封面自动压 JPG≤64KB、正文图压 ≤1MB 转传换微信 URL。账号凭据存
`Account.api_credentials`（AppID/AppSecret，API 永不回传 secret 原文），token 缓存在
`Account.api_token_cache` 跨进程共享；`POST /api/accounts/{id}/verify-credentials` 验证凭据。
注意：微信接口要求服务器出口 IP 在公众平台白名单内，否则 40164。
```

Gotchas 节追加一条：

```markdown
- 微信公众号发布走纯 HTTP（无浏览器），Windows 本地 / CI 可全链路跑；但需要服务器出口
  公网 IP 加入公众平台 IP 白名单（报 40164 时先查这个）。`distribution_enabled=false` 的
  账号会被 pipeline distribute 自动派号过滤（全停用时节点安静跳过）。
```

- [ ] **Step 2: 全量回归 + lint**

```bash
"$PY" -m ruff check server/
"$PY" -m ruff format --check server/
"$PY" -m mypy server/app
"$PY" -m pytest server/tests/ -q
```

Expected: 全部通过（ruff format 报格式问题就去掉 `--check` 改写后重跑）。

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md 登记微信公众号 API 驱动与账号凭据字段"
```

---

## 自审结论（写计划时已核对）

- spec 第 3 节（数据模型）→ Task 1；第 4 节（账号 API）→ Task 4；第 5 节（client/驱动）→ Task 2、6；第 6 节（图片管线）→ Task 3、6；第 7 节（状态映射）→ Task 6；第 8 节（测试）→ 各 task 内嵌；第 9 节（运维）→ Task 7 文档。无遗漏。
- 类型一致性：`fetch_access_token -> tuple[str, int]`、`compress_content_image -> tuple[bytes, str]`、`ApiPublishPayload.cover_path: Path | None`、`is_api_driver(code) -> bool` 在各 task 间签名一致。
- 已知留给执行者核对的点：Task 5 的 `NodeRunContext` 构造参数以 `nodes/base.py` 为准（文中已标注）；Task 4 旧 rename 用例引用 `AccountRenameRequest` 处需同步改名。
