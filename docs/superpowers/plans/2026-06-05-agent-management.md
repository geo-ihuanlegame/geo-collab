# 智能体管理 + 定时调度 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 把 `Pipeline` 扩成可运营管理的「智能体」（type/tags/ignore_exception/is_enabled/调度字段 + 校验 + CRUD），加一个 web 后台 cron 调度器（本地时区、预设档位+时间窗、claim 防重）真实自动触发运行，让 `ignore_exception` 在执行器生效，并新增「智能体管理」导航 tab（置首）+ 管理界面。

**Architecture:** 扩展现有 `pipelines` 模块（不新建实体）。新增 `schedule_calc.py`（纯逻辑判定，单测）+ `scheduler.py`（镜像 `ai_generation/sync_scheduler.py` 的后台线程）。`executor.run_pipeline` 接 `ignore_exception` fail-fast。前端在 `web/src/features/pipelines/` 加 `AgentManagementWorkspace`。

**Tech Stack:** FastAPI + SQLAlchemy + Alembic + MySQL + pytest（后端容器跑）；React 19 + Vite + TS（前端 host pnpm）；`zoneinfo`。

---

## 约定（同前序计划，复述关键点）

- **唯一改动目标 = geo-collab**；参考项目只读禁改。
- **后端在容器**：`docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest <args>'`。宿主无 python。`server/` bind-mount，host 编辑实时生效。
- **ruff 双门禁**：`ruff check server/` **和** `ruff format --check server/`（用 `ruff format` 修）。`Callable` 从 `collections.abc`（UP035）。测试 import 放顶部（E402）。
- **前端在 host**：`pnpm --filter @geo/web typecheck` + `build`，硬门禁。
- **错误**：service 抛 `ValidationError` / `ClientError`（`server.app.shared.errors`），不抛裸 ValueError。
- **后台线程 session**：scheduler 自建 session、本线程 commit/close；触发的 `run_pipeline` 自管 session。
- **迁移**：写前 `ls server/alembic/versions/ | sort | tail -3` 确认 head（现 `0038`），用 `0039`。
- **分支** `feat/agent-management`，逐 Task 提交。
- 现有事实（已核实）：`Pipeline` 字段见 models.py；`PipelineCreate{name,description}` / `PipelinePatch{name,description}` / `PipelineRead{...,nodes}`（schemas.py）；`create_pipeline(db,*,user_id,name,description)` / `patch_pipeline(db,p,*,name,description)`（service.py）；执行器节点循环在 `executor.py:65-101`（`had_failure` 在 95/101 处置位）；settings 在 `core/config.py:Settings`（如 `question_pool_auto_sync_enabled`/`question_pool_sync_interval_seconds`）；`create_app()` 在 `main.py:262-269` 启动 `start_auto_sync(SessionLocal)`。

---

## Task 1: Pipeline 扩字段 + Alembic 0039

**Files:**
- Modify: `server/app/modules/pipelines/models.py`
- Create: `server/alembic/versions/0039_agent_fields.py`

- [ ] **Step 1: 模型加字段**

在 `Pipeline` 类的 `updated_at` 之前/之后加（保持与现有 `Mapped`/`mapped_column` 风格一致）：
```python
    type: Mapped[str] = mapped_column(String(20), default="general", server_default="general")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    ignore_exception: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    schedule_kind: Mapped[str] = mapped_column(String(20), default="none", server_default="none")
    schedule_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schedule_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schedule_weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    window_start: Mapped[Time | None] = mapped_column(Time, nullable=True)
    window_end: Mapped[Time | None] = mapped_column(Time, nullable=True)
    last_scheduled_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
```
顶部 import 补 `Time`：`from sqlalchemy import (... Time ...)`（与现有 import 行合并）。`datetime` 已 import。

- [ ] **Step 2: 确认 head + 写迁移**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && ls server/alembic/versions/ | sort | tail -3'`（应见 `0038_pipelines.py` 为最新；若不是，down_revision 用实际最新）。

写 `server/alembic/versions/0039_agent_fields.py`：
```python
"""agent management fields on pipelines

Revision ID: 0039
Revises: 0038
Create Date: 2026-06-05
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("pipelines", sa.Column("type", sa.String(20), nullable=False, server_default="general"))
    op.add_column("pipelines", sa.Column("tags", sa.JSON(), nullable=True))
    op.add_column("pipelines", sa.Column("ignore_exception", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    op.add_column("pipelines", sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")))
    op.add_column("pipelines", sa.Column("schedule_kind", sa.String(20), nullable=False, server_default="none"))
    op.add_column("pipelines", sa.Column("schedule_minute", sa.Integer(), nullable=True))
    op.add_column("pipelines", sa.Column("schedule_hour", sa.Integer(), nullable=True))
    op.add_column("pipelines", sa.Column("schedule_weekday", sa.Integer(), nullable=True))
    op.add_column("pipelines", sa.Column("window_start", sa.Time(), nullable=True))
    op.add_column("pipelines", sa.Column("window_end", sa.Time(), nullable=True))
    op.add_column("pipelines", sa.Column("last_scheduled_run_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    for col in (
        "last_scheduled_run_at", "window_end", "window_start", "schedule_weekday",
        "schedule_hour", "schedule_minute", "schedule_kind", "is_enabled",
        "ignore_exception", "tags", "type",
    ):
        op.drop_column("pipelines", col)
```

- [ ] **Step 3: 应用 + 验证**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && alembic upgrade head && alembic current'`
Expected: 无报错，current 显示 `0039 (head)`。
Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && python -c "import server.app.modules.pipelines.models; print(\"ok\")"'`

- [ ] **Step 4: 提交**

```bash
git add server/app/modules/pipelines/models.py server/alembic/versions/0039_agent_fields.py
git commit -m "feat(agents): pipeline agent-management columns + migration 0039"
```

---

## Task 2: 校验 + schemas + service + router

**Files:**
- Modify: `server/app/modules/pipelines/service.py`（新增 `validate_agent_fields` + 扩 create/patch）
- Modify: `server/app/modules/pipelines/schemas.py`
- Modify: `server/app/modules/pipelines/router.py`
- Test: `server/tests/test_agent_management.py`（新建）

- [ ] **Step 1: 写失败测试（校验纯逻辑，无 DB）**

```python
# server/tests/test_agent_management.py
import datetime as dt

import pytest

from server.app.modules.pipelines.service import validate_agent_fields
from server.app.shared.errors import ValidationError


def test_validate_ok_minimal():
    validate_agent_fields(name="智能体", type="general", tags=[], schedule_kind="none",
                          schedule_minute=None, schedule_hour=None, schedule_weekday=None,
                          window_start=None, window_end=None)


def test_validate_name_too_long():
    with pytest.raises(ValidationError):
        validate_agent_fields(name="x" * 51, type="general", tags=[], schedule_kind="none",
                              schedule_minute=None, schedule_hour=None, schedule_weekday=None,
                              window_start=None, window_end=None)


def test_validate_bad_type_and_tags():
    with pytest.raises(ValidationError):
        validate_agent_fields(name="a", type="weird", tags=[], schedule_kind="none",
                              schedule_minute=None, schedule_hour=None, schedule_weekday=None,
                              window_start=None, window_end=None)
    with pytest.raises(ValidationError):
        validate_agent_fields(name="a", type="general", tags=["1", "2", "3", "4", "5", "6"],
                              schedule_kind="none", schedule_minute=None, schedule_hour=None,
                              schedule_weekday=None, window_start=None, window_end=None)


def test_validate_schedule_consistency():
    # daily 缺 hour
    with pytest.raises(ValidationError):
        validate_agent_fields(name="a", type="general", tags=[], schedule_kind="daily",
                              schedule_minute=30, schedule_hour=None, schedule_weekday=None,
                              window_start=None, window_end=None)
    # weekly 全齐 OK
    validate_agent_fields(name="a", type="general", tags=[], schedule_kind="weekly",
                          schedule_minute=30, schedule_hour=9, schedule_weekday=0,
                          window_start=None, window_end=None)


def test_validate_window_order():
    with pytest.raises(ValidationError):
        validate_agent_fields(name="a", type="general", tags=[], schedule_kind="none",
                              schedule_minute=None, schedule_hour=None, schedule_weekday=None,
                              window_start=dt.time(20, 0), window_end=dt.time(8, 0))
```

- [ ] **Step 2: 运行，确认失败**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && python -m pytest server/tests/test_agent_management.py -q'`
Expected: FAIL（ImportError validate_agent_fields）

- [ ] **Step 3: 实现 validate_agent_fields（service.py 顶部、create_pipeline 之前）**

```python
VALID_AGENT_TYPES = {"generation", "distribution", "general"}
VALID_SCHEDULE_KINDS = {"none", "hourly", "daily", "weekly"}


def validate_agent_fields(
    *, name, type, tags, schedule_kind,
    schedule_minute, schedule_hour, schedule_weekday, window_start, window_end,
) -> None:
    if not name or not name.strip():
        raise ValidationError("名称不能为空")
    if len(name.strip()) > 50:
        raise ValidationError("名称长度不能超过 50")
    if type not in VALID_AGENT_TYPES:
        raise ValidationError(f"非法类型: {type}")
    if not isinstance(tags, list) or len(tags) > 5:
        raise ValidationError("标签最多 5 个")
    for t in tags:
        if not isinstance(t, str) or not t.strip():
            raise ValidationError("标签不能为空")
    if schedule_kind not in VALID_SCHEDULE_KINDS:
        raise ValidationError(f"非法调度类型: {schedule_kind}")
    if schedule_kind in ("hourly", "daily", "weekly"):
        if schedule_minute is None or not (0 <= schedule_minute <= 59):
            raise ValidationError("分钟需在 0-59")
    if schedule_kind in ("daily", "weekly"):
        if schedule_hour is None or not (0 <= schedule_hour <= 23):
            raise ValidationError("小时需在 0-23")
    if schedule_kind == "weekly":
        if schedule_weekday is None or not (0 <= schedule_weekday <= 6):
            raise ValidationError("星期需在 0-6（周一=0）")
    if (window_start is None) != (window_end is None):
        raise ValidationError("时间窗起止需同时设置或同时留空")
    if window_start is not None and window_end is not None and not (window_start < window_end):
        raise ValidationError("时间窗起须早于止")
```

- [ ] **Step 4: 运行，确认通过**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && python -m pytest server/tests/test_agent_management.py -q'`
Expected: 5 passed。

- [ ] **Step 5: 扩 schemas（schemas.py）**

```python
from datetime import datetime, time  # 顶部 datetime import 改为含 time

# PipelineCreate 改为：
class PipelineCreate(BaseModel):
    name: str
    description: str | None = None
    type: str = "general"
    tags: list[str] = []
    ignore_exception: bool = False
    is_enabled: bool = True
    schedule_kind: str = "none"
    schedule_minute: int | None = None
    schedule_hour: int | None = None
    schedule_weekday: int | None = None
    window_start: time | None = None
    window_end: time | None = None


# PipelinePatch 改为（全部可选，None=不改）：
class PipelinePatch(BaseModel):
    name: str | None = None
    description: str | None = None
    type: str | None = None
    tags: list[str] | None = None
    ignore_exception: bool | None = None
    is_enabled: bool | None = None
    schedule_kind: str | None = None
    schedule_minute: int | None = None
    schedule_hour: int | None = None
    schedule_weekday: int | None = None
    window_start: time | None = None
    window_end: time | None = None


# PipelineRead 加字段（在 nodes 之前）：
    type: str = "general"
    tags: list[str] = []
    ignore_exception: bool = False
    is_enabled: bool = True
    schedule_kind: str = "none"
    schedule_minute: int | None = None
    schedule_hour: int | None = None
    schedule_weekday: int | None = None
    window_start: time | None = None
    window_end: time | None = None
    last_scheduled_run_at: datetime | None = None
```

- [ ] **Step 6: 扩 service create/patch**

`create_pipeline` 改为接收并校验新字段：
```python
def create_pipeline(
    db: Session, *, user_id: int, name: str, description: str | None,
    type: str = "general", tags: list[str] | None = None,
    ignore_exception: bool = False, is_enabled: bool = True,
    schedule_kind: str = "none", schedule_minute: int | None = None,
    schedule_hour: int | None = None, schedule_weekday: int | None = None,
    window_start=None, window_end=None,
) -> Pipeline:
    tags = tags or []
    validate_agent_fields(
        name=name, type=type, tags=tags, schedule_kind=schedule_kind,
        schedule_minute=schedule_minute, schedule_hour=schedule_hour,
        schedule_weekday=schedule_weekday, window_start=window_start, window_end=window_end,
    )
    p = Pipeline(
        user_id=user_id, name=name.strip(), description=description, has_draft=False,
        type=type, tags=tags, ignore_exception=ignore_exception, is_enabled=is_enabled,
        schedule_kind=schedule_kind, schedule_minute=schedule_minute,
        schedule_hour=schedule_hour, schedule_weekday=schedule_weekday,
        window_start=window_start, window_end=window_end,
    )
    db.add(p)
    db.flush()
    return p
```

`patch_pipeline` 改为 overlay 已提供字段后整体校验：
```python
def patch_pipeline(db: Session, p: Pipeline, *, fields: dict) -> Pipeline:
    """fields = PipelinePatch.model_dump(exclude_unset=True)。只覆盖提供的字段。"""
    merged = {
        "name": p.name, "type": p.type, "tags": list(p.tags or []),
        "schedule_kind": p.schedule_kind, "schedule_minute": p.schedule_minute,
        "schedule_hour": p.schedule_hour, "schedule_weekday": p.schedule_weekday,
        "window_start": p.window_start, "window_end": p.window_end,
    }
    for k in merged:
        if k in fields and fields[k] is not None:
            merged[k] = fields[k]
    validate_agent_fields(**merged)
    # 应用（含 description / 开关，None=不改）
    settable = [
        "name", "description", "type", "tags", "ignore_exception", "is_enabled",
        "schedule_kind", "schedule_minute", "schedule_hour", "schedule_weekday",
        "window_start", "window_end",
    ]
    for k in settable:
        if k in fields and fields[k] is not None:
            setattr(p, k, fields[k].strip() if k == "name" else fields[k])
    db.flush()
    return p
```
> 注意 `validate_agent_fields(**merged)` 需要 merged 含全部命名参数——上面 merged 已覆盖校验所需键（name/type/tags/schedule_*/window_*）。`ignore_exception`/`is_enabled`/`description` 不参与校验，单独 set。

- [ ] **Step 7: 改 router（create/patch 端点传字段）**

`router.py` 的 create 端点：
```python
    p = svc.create_pipeline(
        db, user_id=user.id, name=payload.name, description=payload.description,
        type=payload.type, tags=payload.tags, ignore_exception=payload.ignore_exception,
        is_enabled=payload.is_enabled, schedule_kind=payload.schedule_kind,
        schedule_minute=payload.schedule_minute, schedule_hour=payload.schedule_hour,
        schedule_weekday=payload.schedule_weekday,
        window_start=payload.window_start, window_end=payload.window_end,
    )
```
patch 端点：
```python
    svc.patch_pipeline(db, p, fields=payload.model_dump(exclude_unset=True))
```
`_to_read` 已用 `PipelineRead.model_validate(p)`，新字段会自动带出（确认 `_to_read` 是 model_validate；若是手工组 dict 则补字段）。

- [ ] **Step 8: 校验 import + ruff + 既有 pipelines 测试回归**

Run:
```
docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_agent_management.py server/tests/test_pipelines_api.py -q && ruff check server/app/modules/pipelines/ server/tests/test_agent_management.py && ruff format --check server/app/modules/pipelines/ server/tests/test_agent_management.py'
```
Expected: 全 pass + ruff clean。（既有 test_pipelines_api 仍绿，证明 create/patch 兼容。）

- [ ] **Step 9: 提交**

```bash
git add server/app/modules/pipelines/service.py server/app/modules/pipelines/schemas.py server/app/modules/pipelines/router.py server/tests/test_agent_management.py
git commit -m "feat(agents): agent fields validation + CRUD schemas/service/router"
```

---

## Task 3: 调度判定纯逻辑 schedule_calc

**Files:**
- Create: `server/app/modules/pipelines/schedule_calc.py`
- Test: `server/tests/test_agent_management.py`（追加，import 放顶部）

- [ ] **Step 1: 追加失败测试**

```python
# 追加到 server/tests/test_agent_management.py（顶部 import 加 ZoneInfo + current_slot/in_window）
from zoneinfo import ZoneInfo
from server.app.modules.pipelines.schedule_calc import current_slot, in_window

TZ = ZoneInfo("Asia/Shanghai")


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
```

- [ ] **Step 2: 运行，确认失败**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && python -m pytest server/tests/test_agent_management.py -q -k "slot or window"'`
Expected: FAIL（ImportError）

- [ ] **Step 3: 实现 schedule_calc.py**

```python
"""纯逻辑：判定某 pipeline 在给定本地时刻 now 是否到点、所属 slot；时间窗判定。无 DB。"""
from __future__ import annotations

import datetime as dt


def current_slot(
    kind: str, minute: int | None, hour: int | None, weekday: int | None, now: dt.datetime
) -> dt.datetime | None:
    """now 为带本地时区的 datetime。命中返回截到分钟的 slot datetime，否则 None。"""
    if kind == "hourly":
        if minute is not None and now.minute == minute:
            return now.replace(second=0, microsecond=0)
        return None
    if kind == "daily":
        if minute is not None and hour is not None and now.minute == minute and now.hour == hour:
            return now.replace(second=0, microsecond=0)
        return None
    if kind == "weekly":
        if (
            minute is not None and hour is not None and weekday is not None
            and now.minute == minute and now.hour == hour and now.weekday() == weekday
        ):
            return now.replace(second=0, microsecond=0)
        return None
    return None  # none / 未知


def in_window(window_start: dt.time | None, window_end: dt.time | None, now: dt.datetime) -> bool:
    if window_start is None or window_end is None:
        return True
    return window_start <= now.timetz().replace(tzinfo=None) <= window_end
```

- [ ] **Step 4: 运行，确认通过**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && python -m pytest server/tests/test_agent_management.py -q && ruff check server/app/modules/pipelines/schedule_calc.py && ruff format --check server/app/modules/pipelines/schedule_calc.py'`
Expected: 全 pass + ruff clean。

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/pipelines/schedule_calc.py server/tests/test_agent_management.py
git commit -m "test(agents): schedule_calc current_slot + in_window pure logic"
```

---

## Task 4: 调度器 scheduler.py + settings + create_app 接入

**Files:**
- Create: `server/app/modules/pipelines/scheduler.py`
- Modify: `server/app/core/config.py`（加 settings）
- Modify: `server/app/main.py`（create_app 启动）
- Test: `server/tests/test_agent_management.py`（追加 mysql 集成）

- [ ] **Step 1: settings（config.py，Settings 类内，挨着 question_pool_* 加）**

```python
    pipeline_scheduler_enabled: bool = False  # GEO_PIPELINE_SCHEDULER_ENABLED
    pipeline_scheduler_interval_seconds: int = 60  # GEO_PIPELINE_SCHEDULER_INTERVAL_SECONDS
    scheduler_tz: str = "Asia/Shanghai"  # GEO_SCHEDULER_TZ
```

- [ ] **Step 2: 写失败集成测试（@pytest.mark.mysql）**

```python
# 追加到 server/tests/test_agent_management.py
import pytest
from server.tests.utils import build_test_app


def _publish_simple_pipeline(client, name="定时体", schedule=None):
    body = {"name": name}
    if schedule:
        body.update(schedule)
    pid = client.post("/api/pipelines", json=body).json()["id"]
    snap = {"schemaVersion": 1, "nodes": [
        {"node_type": "input", "name": "源", "node_index": 0,
         "config": {"question_text": "x"}, "flow_meta": None}]}
    client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snap})
    client.post(f"/api/pipelines/{pid}/publish", json={})
    return pid


@pytest.mark.mysql
def test_run_due_triggers_once_and_claims(monkeypatch):
    triggered = []
    monkeypatch.setattr(
        "server.app.modules.pipelines.scheduler.run_pipeline",
        lambda run_id, sf: triggered.append(run_id))
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        pid = _publish_simple_pipeline(client, schedule={
            "schedule_kind": "daily", "schedule_minute": 30, "schedule_hour": 9, "is_enabled": True})
        from server.app.modules.pipelines.scheduler import run_due_pipelines_once
        now = dt.datetime(2026, 6, 5, 9, 30, tzinfo=TZ)
        n1 = run_due_pipelines_once(test_app.session_factory, now=now)
        assert n1 == 1 and len(triggered) == 1
        # 同 slot 再跑：claim 幂等
        n2 = run_due_pipelines_once(test_app.session_factory, now=now)
        assert n2 == 0
        from server.app.modules.pipelines.models import Pipeline
        with test_app.session_factory() as db:
            assert db.get(Pipeline, pid).last_scheduled_run_at is not None
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_run_due_skips_disabled_window_and_no_nodes(monkeypatch):
    monkeypatch.setattr(
        "server.app.modules.pipelines.scheduler.run_pipeline", lambda run_id, sf: None)
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        from server.app.modules.pipelines.scheduler import run_due_pipelines_once
        now = dt.datetime(2026, 6, 5, 9, 30, tzinfo=TZ)
        # disabled
        _publish_simple_pipeline(client, name="停用", schedule={
            "schedule_kind": "daily", "schedule_minute": 30, "schedule_hour": 9, "is_enabled": False})
        # 窗外
        _publish_simple_pipeline(client, name="窗外", schedule={
            "schedule_kind": "daily", "schedule_minute": 30, "schedule_hour": 9,
            "window_start": "10:00:00", "window_end": "23:00:00"})
        # 无已发布节点（建但不发布）
        pid3 = client.post("/api/pipelines", json={
            "name": "无节点", "schedule_kind": "daily", "schedule_minute": 30, "schedule_hour": 9}).json()["id"]
        assert run_due_pipelines_once(test_app.session_factory, now=now) == 0
    finally:
        test_app.cleanup()
```

- [ ] **Step 3: 运行，确认失败**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_agent_management.py -q -k run_due'`
Expected: FAIL（ImportError scheduler）

- [ ] **Step 4: 实现 scheduler.py**

```python
"""Pipeline 定时调度：镜像 ai_generation.sync_scheduler。run_due_pipelines_once 纯函数式可测，
后台线程只负责 wait→run_once。create_app 在 GEO_PIPELINE_SCHEDULER_ENABLED 时启动。"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import update

from server.app.core.config import get_settings
from server.app.modules.pipelines.executor import create_run, run_pipeline
from server.app.modules.pipelines.models import Pipeline, PipelineNode, PipelineRun
from server.app.modules.pipelines.schedule_calc import current_slot, in_window

logger = logging.getLogger(__name__)
SessionFactory = Callable[[], Any]

_stop = threading.Event()
_thread: threading.Thread | None = None


def _to_utc_naive(slot_local: dt.datetime) -> dt.datetime:
    return slot_local.astimezone(dt.timezone.utc).replace(tzinfo=None)


def run_due_pipelines_once(session_factory: SessionFactory, now: dt.datetime | None = None) -> int:
    """扫描到期 pipeline 并触发。now 为带本地时区 datetime（默认按 GEO_SCHEDULER_TZ 取当前）。
    返回触发数。best-effort：单个失败只记日志。"""
    if now is None:
        now = dt.datetime.now(ZoneInfo(get_settings().scheduler_tz))
    triggered = 0
    db = session_factory()
    try:
        candidates = (
            db.query(Pipeline)
            .filter(Pipeline.is_enabled.is_(True), Pipeline.schedule_kind != "none")
            .all()
        )
        rows = [
            (p.id, p.schedule_kind, p.schedule_minute, p.schedule_hour, p.schedule_weekday,
             p.window_start, p.window_end)
            for p in candidates
        ]
    finally:
        db.close()

    for (pid, kind, minute, hour, weekday, w_start, w_end) in rows:
        try:
            slot_local = current_slot(kind, minute, hour, weekday, now)
            if slot_local is None or not in_window(w_start, w_end, now):
                continue
            slot_utc = _to_utc_naive(slot_local)
            db = session_factory()
            try:
                # 无已发布节点 → 跳过
                has_nodes = db.query(PipelineNode.id).filter(PipelineNode.pipeline_id == pid).first()
                if has_nodes is None:
                    continue
                # 运行中不重叠
                running = (
                    db.query(PipelineRun.id)
                    .filter(PipelineRun.pipeline_id == pid,
                            PipelineRun.status.in_(("pending", "running")))
                    .first()
                )
                if running is not None:
                    continue
                # claim：条件 UPDATE，rowcount==1 才算抢到
                res = db.execute(
                    update(Pipeline)
                    .where(
                        Pipeline.id == pid,
                        (Pipeline.last_scheduled_run_at.is_(None))
                        | (Pipeline.last_scheduled_run_at < slot_utc),
                    )
                    .values(last_scheduled_run_at=slot_utc)
                )
                db.commit()
                if res.rowcount != 1:
                    continue
                p = db.get(Pipeline, pid)
                run = create_run(db, pipeline_id=pid, user_id=p.user_id)
                db.commit()
                run_id = run.id
            finally:
                db.close()
            threading.Thread(
                target=run_pipeline, args=(run_id, session_factory), daemon=True
            ).start()
            triggered += 1
        except Exception:  # noqa: BLE001
            logger.exception("scheduler: pipeline %s trigger failed", pid)
    return triggered


def start_pipeline_scheduler(session_factory: SessionFactory) -> bool:
    global _thread
    if _thread is not None and _thread.is_alive():
        return False
    _stop.clear()

    def _loop() -> None:
        while not _stop.is_set():
            try:
                run_due_pipelines_once(session_factory)
            except Exception:  # noqa: BLE001
                logger.exception("pipeline scheduler loop error")
            interval = max(30, get_settings().pipeline_scheduler_interval_seconds)
            if _stop.wait(interval):
                break

    _thread = threading.Thread(target=_loop, daemon=True, name="pipeline-scheduler")
    _thread.start()
    return True


def stop_pipeline_scheduler() -> None:
    _stop.set()
```

- [ ] **Step 5: create_app 启动（main.py，紧随 start_auto_sync 的 try 块之后加同款 try 块）**

```python
    try:
        from server.app.modules.pipelines.scheduler import start_pipeline_scheduler

        if get_settings().pipeline_scheduler_enabled:
            start_pipeline_scheduler(SessionLocal)
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).exception("start_pipeline_scheduler failed")
```
> 确认 `get_settings` 在 main.py 已 import（若无则补 `from server.app.core.config import get_settings`）。

- [ ] **Step 6: 运行集成测试 + ruff**

Run:
```
docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_agent_management.py -q && ruff check server/app/modules/pipelines/scheduler.py server/app/core/config.py server/app/main.py && ruff format --check server/app/modules/pipelines/scheduler.py'
```
Expected: 全 pass + ruff clean。

- [ ] **Step 7: create_app 仍能加载**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && python -c "from server.app.main import create_app; create_app(); print(\"ok\")"'`

- [ ] **Step 8: 提交**

```bash
git add server/app/modules/pipelines/scheduler.py server/app/core/config.py server/app/main.py server/tests/test_agent_management.py
git commit -m "feat(agents): pipeline cron scheduler (local tz, claim, window) + app wiring"
```

---

## Task 5: executor 接 ignore_exception（fail-fast）

**Files:**
- Modify: `server/app/modules/pipelines/executor.py`
- Test: `server/tests/test_agent_management.py`（追加）

- [ ] **Step 1: 追加失败测试（双节点，第一个失败）**

用一个会抛错的临时节点类型。最简：复用 `ai_generate` 配一个不存在的 prompt_template_id → 节点抛 ValidationError（第一个节点失败），第二个节点是 input。
```python
@pytest.mark.mysql
def test_ignore_exception_fail_fast_vs_continue(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        from server.app.modules.pipelines.executor import create_run, run_pipeline
        from server.app.modules.pipelines.models import Pipeline

        def _build(ignore: bool):
            pid = client.post("/api/pipelines", json={
                "name": f"ie-{ignore}", "ignore_exception": ignore}).json()["id"]
            snap = {"schemaVersion": 1, "nodes": [
                {"node_type": "ai_generate", "name": "坏", "node_index": 0,
                 "config": {"prompt_template_id": 999999, "count": 1, "question_text": "x"},
                 "flow_meta": None},
                {"node_type": "input", "name": "后", "node_index": 1,
                 "config": {"question_text": "y"}, "flow_meta": None}]}
            client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snap})
            client.post(f"/api/pipelines/{pid}/publish", json={})
            with test_app.session_factory() as db:
                p = db.get(Pipeline, pid)
                run = create_run(db, pipeline_id=pid, user_id=p.user_id); db.commit(); rid = run.id
            run_pipeline(rid, test_app.session_factory)
            return client.get(f"/api/pipelines/runs/{rid}").json()

        # fail-fast：第二个节点不应执行
        r_off = _build(False)
        assert r_off["status"] == "failed"
        assert "1" not in r_off["node_results"]  # 后续节点没跑
        # 继续：第二个节点应执行
        r_on = _build(True)
        assert "1" in r_on["node_results"]
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 运行，确认失败**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_agent_management.py -q -k ignore_exception'`
Expected: FAIL（当前执行器总是继续，"1" 在 node_results 里）

- [ ] **Step 3: 改 executor.run_pipeline**

(a) 读 ignore_exception：在加载 nodes 的 session 块里（`pipeline_id, user_id = ...` 之后）加：
```python
        pipeline = db.get(Pipeline, pipeline_id)
        ignore_exception = bool(pipeline.ignore_exception) if pipeline is not None else False
```
顶部 import 补 `Pipeline`（executor 已 import `PipelineNode, PipelineRun`；改为含 `Pipeline`）。

(b) 节点循环：把成功分支里的 errors 检测与 except 分支统一成 `node_failed`，循环末尾 fail-fast。改 `executor.py:79-101` 段为：
```python
        inputs = apply_input_mapping(meta, upstream)
        node_failed = False
        try:
            handler = get_handler(spec["node_type"])
            result = handler(
                NodeRunContext(
                    session_factory=session_factory,
                    user_id=user_id,
                    config=spec["config"],
                    inputs=inputs,
                    upstream=upstream,
                )
            )
            context[idx] = result.output
            node_results[str(idx)] = result.output
            article_ids.extend(result.article_ids)
            if result.output.get("errors"):
                had_failure = True
                node_failed = True
            if result.article_ids or spec["node_type"] == "input":
                had_success = True
        except Exception as exc:
            logger.exception("pipeline run %s node #%s failed", run_id, idx)
            node_results[str(idx)] = {"error": str(exc)}
            had_failure = True
            node_failed = True

        if node_failed and not ignore_exception:
            break  # fail-fast：停掉后续节点
```

- [ ] **Step 4: 运行，确认通过 + 既有回归**

Run:
```
docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_agent_management.py server/tests/test_pipelines_api.py server/tests/test_pipeline_review_distribute.py -q && ruff check server/app/modules/pipelines/executor.py && ruff format --check server/app/modules/pipelines/executor.py'
```
Expected: 全 pass + ruff clean。**特别确认 test_pipeline_review_distribute 的门禁用例仍 failed**（distribute 单节点失败，无后续节点，fail-fast 不影响其断言）。

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/pipelines/executor.py server/tests/test_agent_management.py
git commit -m "feat(agents): executor honors ignore_exception (fail-fast default)"
```

---

## Task 6: 前端类型 + API + 导航 tab 置首

**Files:**
- Modify: `web/src/types.ts`（Pipeline 扩字段 + NavKey + navItems）
- Modify: `web/src/api/pipelines.ts`（create/patch 扩字段）
- Modify: `web/src/App.tsx`
- Create: `web/src/features/pipelines/AgentManagementWorkspace.tsx`（占位，Task 7 完善）

- [ ] **Step 1: types.ts — Pipeline 扩字段**

`Pipeline` interface 加：
```typescript
  type: string;
  tags: string[];
  ignore_exception: boolean;
  is_enabled: boolean;
  schedule_kind: string;
  schedule_minute: number | null;
  schedule_hour: number | null;
  schedule_weekday: number | null;
  window_start: string | null;
  window_end: string | null;
  last_scheduled_run_at: string | null;
```

- [ ] **Step 2: types.ts — NavKey + navItems 置首**

`NavKey` 加 `"agents"`。`navItems` 数组**第一项**插入：
```typescript
import { Bot } from "lucide-react";  // 确认存在，否则用 Boxes/Cpu
// navItems 顶部：
{ key: "agents", label: "智能体管理", icon: Bot },
```
（"工作流编排" 项保持其后。）

- [ ] **Step 3: api/pipelines.ts — create/patch payload 扩字段**

把 `createPipeline` / `patchPipeline` 的入参类型扩展为可带新字段（用 `Partial<...>`）：
```typescript
export interface AgentFields {
  type?: string; tags?: string[]; ignore_exception?: boolean; is_enabled?: boolean;
  schedule_kind?: string; schedule_minute?: number | null; schedule_hour?: number | null;
  schedule_weekday?: number | null; window_start?: string | null; window_end?: string | null;
}
export const createPipeline = (p: { name: string; description?: string } & AgentFields) =>
  api<Pipeline>("/api/pipelines", { method: "POST", body: JSON.stringify(p) });
export const patchPipeline = (id: number, p: { name?: string; description?: string } & AgentFields) =>
  api<Pipeline>(`/api/pipelines/${id}`, { method: "PATCH", body: JSON.stringify(p) });
```

- [ ] **Step 4: App.tsx 渲染块 + 占位 workspace**

`App.tsx` 加（mirror 现有 tab 块）：
```tsx
import { AgentManagementWorkspace } from "./features/pipelines/AgentManagementWorkspace";
// workspace 区，置于其它 tab 之前：
{visitedTabs.has("agents") && (
  <div style={{ display: activeNav === "agents" ? undefined : "none" }}>
    <ErrorBoundary fallback={<p role="alert">智能体管理出错，请刷新重试</p>}>
      <AgentManagementWorkspace onEditFlow={() => handleNavClick("pipelines")} />
    </ErrorBoundary>
  </div>
)}
```
占位组件：
```tsx
// web/src/features/pipelines/AgentManagementWorkspace.tsx
export function AgentManagementWorkspace({ onEditFlow }: { onEditFlow: (id: number) => void }) {
  void onEditFlow;
  return <div>智能体管理（占位）</div>;
}
```

- [ ] **Step 5: typecheck + build**

Run（host）：`pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 通过；导航首位出现「智能体管理」。

- [ ] **Step 6: 提交**

```bash
git add web/src/types.ts web/src/api/pipelines.ts web/src/App.tsx web/src/features/pipelines/AgentManagementWorkspace.tsx
git commit -m "feat(agents): frontend types/api + 智能体管理 nav tab (first)"
```

---

## Task 7: 智能体管理界面（列表 + 表单 + 调度选择器）

**Files:**
- Modify: `web/src/features/pipelines/AgentManagementWorkspace.tsx`（替换占位）

- [ ] **Step 1: 实现完整 workspace**

```tsx
// web/src/features/pipelines/AgentManagementWorkspace.tsx
import { useCallback, useEffect, useState } from "react";
import {
  createPipeline, deletePipeline, listPipelines, patchPipeline, startRun,
} from "../../api/pipelines";
import { useToast } from "../../components/Toast";
import type { Pipeline } from "../../types";

const TYPES = [
  { v: "general", label: "通用" },
  { v: "generation", label: "生成型" },
  { v: "distribution", label: "分发型" },
];
const KINDS = [
  { v: "none", label: "不定时" },
  { v: "hourly", label: "每小时" },
  { v: "daily", label: "每天" },
  { v: "weekly", label: "每周" },
];
const WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

type FormState = {
  id: number | null; name: string; type: string; tagsText: string;
  ignore_exception: boolean; is_enabled: boolean; schedule_kind: string;
  schedule_minute: number; schedule_hour: number; schedule_weekday: number;
  window_start: string; window_end: string;
};
const EMPTY: FormState = {
  id: null, name: "", type: "general", tagsText: "", ignore_exception: false,
  is_enabled: true, schedule_kind: "none", schedule_minute: 0, schedule_hour: 9,
  schedule_weekday: 0, window_start: "", window_end: "",
};

function scheduleSummary(p: Pipeline): string {
  if (p.schedule_kind === "none") return "—";
  const mm = String(p.schedule_minute ?? 0).padStart(2, "0");
  const hh = String(p.schedule_hour ?? 0).padStart(2, "0");
  if (p.schedule_kind === "hourly") return `每小时 :${mm}`;
  if (p.schedule_kind === "daily") return `每天 ${hh}:${mm}`;
  if (p.schedule_kind === "weekly") return `${WEEKDAYS[p.schedule_weekday ?? 0]} ${hh}:${mm}`;
  return "—";
}

export function AgentManagementWorkspace({ onEditFlow }: { onEditFlow: (id: number) => void }) {
  const { toast } = useToast();
  const [items, setItems] = useState<Pipeline[]>([]);
  const [form, setForm] = useState<FormState | null>(null);

  const reload = useCallback(async () => {
    try { setItems(await listPipelines()); }
    catch (e) { toast(e instanceof Error ? e.message : "加载失败", "error"); }
  }, [toast]);
  useEffect(() => { reload(); }, [reload]);

  const openCreate = () => setForm({ ...EMPTY });
  const openEdit = (p: Pipeline) => setForm({
    id: p.id, name: p.name, type: p.type, tagsText: (p.tags || []).join(","),
    ignore_exception: p.ignore_exception, is_enabled: p.is_enabled,
    schedule_kind: p.schedule_kind, schedule_minute: p.schedule_minute ?? 0,
    schedule_hour: p.schedule_hour ?? 9, schedule_weekday: p.schedule_weekday ?? 0,
    window_start: p.window_start ?? "", window_end: p.window_end ?? "",
  });

  const buildPayload = (f: FormState) => {
    const tags = f.tagsText.split(",").map((s) => s.trim()).filter(Boolean);
    const base: Record<string, unknown> = {
      name: f.name, type: f.type, tags, ignore_exception: f.ignore_exception,
      is_enabled: f.is_enabled, schedule_kind: f.schedule_kind,
      window_start: f.window_start || null, window_end: f.window_end || null,
      schedule_minute: null, schedule_hour: null, schedule_weekday: null,
    };
    if (["hourly", "daily", "weekly"].includes(f.schedule_kind)) base.schedule_minute = f.schedule_minute;
    if (["daily", "weekly"].includes(f.schedule_kind)) base.schedule_hour = f.schedule_hour;
    if (f.schedule_kind === "weekly") base.schedule_weekday = f.schedule_weekday;
    return base;
  };

  const save = async () => {
    if (!form) return;
    try {
      const payload = buildPayload(form);
      if (form.id == null) await createPipeline(payload as { name: string });
      else await patchPipeline(form.id, payload);
      setForm(null); reload(); toast("已保存", "success");
    } catch (e) { toast(e instanceof Error ? e.message : "保存失败", "error"); }
  };

  const remove = async (p: Pipeline) => {
    if (!window.confirm(`确认删除智能体「${p.name}」？此操作不可撤销。`)) return;
    try { await deletePipeline(p.id); reload(); } catch (e) { toast(e instanceof Error ? e.message : "删除失败", "error"); }
  };

  const runNow = async (p: Pipeline) => {
    try { await startRun(p.id); toast("已触发运行", "success"); }
    catch (e) { toast(e instanceof Error ? e.message : "运行失败（需先发布节点）", "error"); }
  };

  return (
    <div className="agentsWorkspace">
      <div className="topbar"><div><p className="eyebrow">智能体</p><h1>智能体管理</h1></div>
        <button onClick={openCreate}>+ 新建智能体</button></div>

      <table style={{ width: "100%" }}>
        <thead><tr>
          <th>名称</th><th>类型</th><th>标签</th><th>调度（北京时间）</th><th>启用</th><th>操作</th>
        </tr></thead>
        <tbody>
          {items.map((p) => (
            <tr key={p.id}>
              <td>{p.name}{p.has_draft ? " ●" : ""}</td>
              <td>{TYPES.find((t) => t.v === p.type)?.label ?? p.type}</td>
              <td>{(p.tags || []).join("、")}</td>
              <td>{scheduleSummary(p)}</td>
              <td>{p.is_enabled ? "是" : "否"}</td>
              <td>
                <button onClick={() => openEdit(p)}>编辑</button>
                <button onClick={() => onEditFlow(p.id)}>编辑流程</button>
                <button onClick={() => runNow(p)}>立即运行</button>
                <button onClick={() => remove(p)}>删除</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {form && (
        <div className="agentForm" style={{ border: "1px solid #ccc", padding: 12, marginTop: 12 }}>
          <h3>{form.id == null ? "新建智能体" : "编辑智能体"}</h3>
          <label>名称<input value={form.name} maxLength={50}
            onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
          <label>类型
            <select value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })}>
              {TYPES.map((t) => <option key={t.v} value={t.v}>{t.label}</option>)}
            </select></label>
          <label>标签（逗号分隔，≤5）
            <input value={form.tagsText} onChange={(e) => setForm({ ...form, tagsText: e.target.value })} /></label>
          <label><input type="checkbox" checked={form.ignore_exception}
            onChange={(e) => setForm({ ...form, ignore_exception: e.target.checked })} /> 异常忽略（出错继续后续节点）</label>
          <label><input type="checkbox" checked={form.is_enabled}
            onChange={(e) => setForm({ ...form, is_enabled: e.target.checked })} /> 启用</label>
          <hr />
          <label>调度（北京时间）
            <select value={form.schedule_kind} onChange={(e) => setForm({ ...form, schedule_kind: e.target.value })}>
              {KINDS.map((k) => <option key={k.v} value={k.v}>{k.label}</option>)}
            </select></label>
          {form.schedule_kind === "weekly" && (
            <label>星期
              <select value={form.schedule_weekday}
                onChange={(e) => setForm({ ...form, schedule_weekday: Number(e.target.value) })}>
                {WEEKDAYS.map((w, i) => <option key={i} value={i}>{w}</option>)}
              </select></label>
          )}
          {["daily", "weekly"].includes(form.schedule_kind) && (
            <label>时<input type="number" min={0} max={23} value={form.schedule_hour}
              onChange={(e) => setForm({ ...form, schedule_hour: Number(e.target.value) })} /></label>
          )}
          {["hourly", "daily", "weekly"].includes(form.schedule_kind) && (
            <label>分<input type="number" min={0} max={59} value={form.schedule_minute}
              onChange={(e) => setForm({ ...form, schedule_minute: Number(e.target.value) })} /></label>
          )}
          <label>时间窗起<input type="time" value={form.window_start}
            onChange={(e) => setForm({ ...form, window_start: e.target.value ? e.target.value + ":00" : "" })} /></label>
          <label>时间窗止<input type="time" value={form.window_end}
            onChange={(e) => setForm({ ...form, window_end: e.target.value ? e.target.value + ":00" : "" })} /></label>
          <div style={{ marginTop: 8 }}>
            <button onClick={save}>保存</button>
            <button onClick={() => setForm(null)}>取消</button>
          </div>
        </div>
      )}
    </div>
  );
}
```
> `window_start/end` 后端要 `HH:MM:SS`，`<input type=time>` 给 `HH:MM`，故存时补 `:00`，回填时 `p.window_start`（已是 `HH:MM:SS`）直接塞给 input 的 value（浏览器接受 `HH:MM:SS`）。若回填显示异常，截前 5 位：`(p.window_start ?? "").slice(0,5)`——实现时择一并保持一致。

- [ ] **Step 2: typecheck + build**

Run（host）：`pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 通过。

- [ ] **Step 3: 手动冒烟（可选）**

启动前后端：新建智能体（名称>50 应被后端拒、toast 报错）→ 设每天 09:30 → 保存 → 列表显示"每天 09:30"→ 立即运行（需先在工作流编排发布节点）→ 删除有二次确认。

- [ ] **Step 4: 提交**

```bash
git add web/src/features/pipelines/AgentManagementWorkspace.tsx
git commit -m "feat(agents): agent management workspace (list/form/schedule selector)"
```

---

## Self-Review 结果

- **Spec 覆盖**：§3 模型=Task1;§4 校验=Task2;§5 调度=Task3(纯逻辑)+Task4(调度器/settings/接入);§6 ignore_exception=Task5;§7 API=Task2;§8 前端=Task6/Task7;§9 测试=各 Task 的单测/集成 + 前端门禁;§12 验收 1=Task2,2=Task4,3=Task5,4=Task6/7,5=不建表(全程扩 Pipeline)。无遗漏。
- **占位符**：无 TBD;每步给完整代码;对未读精确处（main.py get_settings import、_to_read 是否 model_validate、lucide Bot 图标、window time 回填）给"先确认/择一"指令而非假设。
- **类型一致**：`validate_agent_fields(*, name,type,tags,schedule_kind,schedule_minute,schedule_hour,schedule_weekday,window_start,window_end)` 在 Task2 定义、Task2 create/patch 调用一致；`current_slot(kind,minute,hour,weekday,now)` / `in_window(start,end,now)` 在 Task3 定义、Task4 调用一致；`run_due_pipelines_once(session_factory, now=None)` Task4 定义、测试调用一致；前端 `Pipeline` 扩字段（Task6）与表单/摘要（Task7）一致；调度字段名后端(model/schemas)↔前端一致。
- **待核对点（执行时 grep）**：`_to_read` 实现形态、main.py 现有 import、lucide 图标名、executor 顶部 import 是否含 Pipeline、time 字段回填格式。
