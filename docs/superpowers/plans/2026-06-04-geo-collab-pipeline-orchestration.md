# geo-collab 可视化流程编排引擎 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 geo-collab 主仓库新建 `pipelines` 线性编排引擎 + `input`/`ai_generate` 两个内置节点（复用现有生文），支持草稿暂存、版本回溯、连线依赖/数据传递，并新增「工作流编排」导航 tab。

**Architecture:** 后端新模块 `server/app/modules/pipelines/`（models/schemas/service/router + nodes 注册表 + executor），4 张表接 Alembic head `0036`，执行走后台线程 + session-per-step（镜像 `scheme_executor`）。前端新 feature `web/src/features/pipelines/` + `api/pipelines.ts` + 导航 tab。纯逻辑（数据传递求值、快照编解码、节点注册表）以 pytest 单测驱动；DB/执行走 `@pytest.mark.mysql` 集成测试（monkeypatch 掉 LLM）。

**Tech Stack:** 后端 Python / FastAPI / SQLAlchemy(Mapped) / Alembic / MySQL / pytest；前端 React 19 + Vite + TS + lucide。

---

## 约定（零上下文工程师必读）

- **唯一改动目标 = geo-collab 主仓库**。`content-library-public` / `pc-admin-conetnt-library-public` 是**只读参考项目，禁止编辑**。
- 工作目录：`c:\Users\admin\Desktop\geo-collab`。Python 命令在 dev 容器/已激活 `conda activate geo_xzpt` 的环境跑（宿主无 conda）。
- **后端基类与工具**：`from server.app.db.base import Base`；`from server.app.core.time import utcnow`；模型用 `from sqlalchemy.orm import Mapped, mapped_column`，JSON 列用 `sqlalchemy.JSON`。
- **鉴权 / DB 依赖**：`from server.app.core.security import get_current_user`、`from server.app.db.session import get_db`、`from server.app.modules.system.models import User`。
- **错误**：service 层抛 `from server.app.shared.errors import ClientError, ConflictError, ValidationError`（**禁止裸 ValueError**，无全局兜底）。router 内"找不到/无权"用 `HTTPException(404)`（参照 `ai_generation/router.py:_get_owned_pool`）。
- **后台线程注入**：`create_app()` 里 `xxx_router.bg_session_factory = SessionLocal`（参照 `main.py` 对 scheme_router 的注入）。执行器从该 factory 取 session，**每个节点自建/关闭 session**，禁止跨线程传 session。
- **生文复用**：`from server.app.modules.ai_generation.article_writer import generate_article_from_prompt`，签名 `(*, session_factory, user_id, template_content, question_text, model=None) -> int`（返回 article_id，异常上抛）。
- **模板读取**：`from server.app.modules.prompt_templates.service import get_prompt_template`（`(db, template_id) -> PromptTemplate | None`）。`PromptTemplate` 有 `.content / .scope / .is_enabled / .is_deleted`，有效条件：非 None、`scope=="generation"`、`is_enabled`、`not is_deleted`。
- **迁移 head**：写迁移前先 `ls server/alembic/versions/` 确认实际最新（现为 `0036`），不要写死过期版本。
- **测试**：纯逻辑测试无需 DB（直接 `pytest`）；DB 测试加 `@pytest.mark.mysql` + `build_test_app(monkeypatch)`（`from server.tests.utils import build_test_app`），需 `GEO_TEST_DATABASE_URL`（库名含 `test`），`finally` 里 `test_app.cleanup()`。
- **前端**：`pnpm --filter @geo/web typecheck` + `pnpm --filter @geo/web build` 为硬门禁。API 走 `web/src/api/core.ts` 的 `api<T>(path, init?)`。
- **提交**：每个 Task 末尾提交，`feat(pipelines): ...` / `test(pipelines): ...`。当前分支 `feat/pipline-visual-orchestration`。

---

## Phase 1 — 后端纯逻辑（pytest TDD，无 DB）

### Task 1: 模块骨架 + 数据传递求值器

**Files:**
- Create: `server/app/modules/pipelines/__init__.py`（空）
- Create: `server/app/modules/pipelines/flow_meta.py`
- Test: `server/tests/test_pipeline_logic.py`

- [ ] **Step 1: 写失败测试**

```python
# server/tests/test_pipeline_logic.py
from server.app.modules.pipelines.flow_meta import apply_input_mapping, should_skip


def test_apply_input_mapping_copies_upstream_to_target_names():
    meta = {"inputMapping": [{"from": "title", "to": "question_text"}]}
    out = apply_input_mapping(meta, {"title": "Hello"})
    assert out == {"question_text": "Hello"}


def test_apply_input_mapping_none_meta_returns_empty():
    assert apply_input_mapping(None, {"a": "b"}) == {}
    assert apply_input_mapping({}, {"a": "b"}) == {}


def test_should_skip_eq_met_false_not_met_true():
    meta = {"condition": {"field": "status", "op": "eq", "value": "ok"}}
    assert should_skip(meta, {"status": "ok"}) is False
    assert should_skip(meta, {"status": "bad"}) is True


def test_should_skip_no_condition_false():
    assert should_skip({}, {}) is False
    assert should_skip(None, {}) is False


def test_should_skip_neq_and_contains():
    meta = {"condition": {"field": "tags", "op": "contains", "value": "news"}}
    assert should_skip(meta, {"tags": "hot,news"}) is False
    assert should_skip(meta, {"tags": "hot"}) is True
    meta = {"condition": {"field": "tags", "op": "neq", "value": "x"}}
    assert should_skip(meta, {"tags": "y"}) is False
    assert should_skip(meta, {"tags": "x"}) is True
```

- [ ] **Step 2: 运行，确认失败**

Run: `pytest server/tests/test_pipeline_logic.py -q`
Expected: FAIL（ModuleNotFoundError: flow_meta）

- [ ] **Step 3: 实现**

```python
# server/app/modules/pipelines/flow_meta.py
"""纯逻辑：节点间数据传递（inputMapping）与跳过条件（condition）。无 DB 依赖。"""
from __future__ import annotations

from typing import Any


def apply_input_mapping(meta: dict | None, upstream: dict[str, Any] | None) -> dict[str, Any]:
    """按 meta.inputMapping 把上游字段拷到目标字段名。meta/mapping/upstream 空则返回 {}。"""
    out: dict[str, Any] = {}
    if not meta or not upstream:
        return out
    for m in meta.get("inputMapping") or []:
        src, dst = m.get("from"), m.get("to")
        if src and dst and src in upstream:
            out[dst] = upstream[src]
    return out


def should_skip(meta: dict | None, ctx: dict[str, Any] | None) -> bool:
    """condition 不满足则返回 True（跳过本节点）。无 condition 永不跳过。op∈eq/neq/contains。"""
    if not meta:
        return False
    cond = meta.get("condition")
    if not cond or not cond.get("field"):
        return False
    actual = "" if ctx is None or ctx.get(cond["field"]) is None else str(ctx.get(cond["field"]))
    expected = cond.get("value") or ""
    op = cond.get("op") or "eq"
    if op == "neq":
        met = actual != expected
    elif op == "contains":
        met = expected in actual
    else:  # eq
        met = actual == expected
    return not met
```

并创建空文件 `server/app/modules/pipelines/__init__.py`。

- [ ] **Step 4: 运行，确认通过**

Run: `pytest server/tests/test_pipeline_logic.py -q`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/pipelines/__init__.py server/app/modules/pipelines/flow_meta.py server/tests/test_pipeline_logic.py
git commit -m "test(pipelines): flow-meta input-mapping and skip-condition"
```

---

### Task 2: 快照编解码器

**Files:**
- Create: `server/app/modules/pipelines/snapshot.py`
- Test: `server/tests/test_pipeline_logic.py`（追加）

- [ ] **Step 1: 追加失败测试**

```python
# 追加到 server/tests/test_pipeline_logic.py
from server.app.modules.pipelines.snapshot import nodes_to_snapshot, snapshot_to_node_dicts


class _FakeNode:
    def __init__(self, node_type, name, node_index, config, flow_meta):
        self.node_type, self.name, self.node_index = node_type, name, node_index
        self.config, self.flow_meta = config, flow_meta


def test_snapshot_round_trip_preserves_order_and_fields():
    nodes = [
        _FakeNode("input", "源", 0, {"question_text": "Q"}, None),
        _FakeNode("ai_generate", "生文", 1, {"prompt_template_id": 5, "count": 2},
                  {"schemaVersion": 1, "inputMapping": [{"from": "question_text", "to": "question_text"}]}),
    ]
    snap = nodes_to_snapshot(nodes)
    assert snap["schemaVersion"] == 1
    assert [n["node_index"] for n in snap["nodes"]] == [0, 1]

    dicts = snapshot_to_node_dicts(snap)
    assert dicts[0]["node_type"] == "input"
    assert dicts[1]["config"]["count"] == 2
    assert dicts[1]["flow_meta"]["inputMapping"][0]["to"] == "question_text"


def test_snapshot_to_node_dicts_handles_empty():
    assert snapshot_to_node_dicts(None) == []
    assert snapshot_to_node_dicts({}) == []
```

- [ ] **Step 2: 运行，确认失败**

Run: `pytest server/tests/test_pipeline_logic.py -q -k snapshot`
Expected: FAIL（ImportError）

- [ ] **Step 3: 实现**

```python
# server/app/modules/pipelines/snapshot.py
"""纯逻辑：pipeline_nodes <-> 快照 dict 互转。"""
from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 1


def nodes_to_snapshot(nodes: list[Any]) -> dict:
    """已发布节点（按 node_index 顺序传入）-> 快照 dict。"""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "nodes": [
            {
                "node_type": n.node_type,
                "name": n.name,
                "node_index": n.node_index,
                "config": n.config or {},
                "flow_meta": n.flow_meta,
            }
            for n in nodes
        ],
    }


def snapshot_to_node_dicts(snapshot: dict | None) -> list[dict]:
    """快照 dict -> 可用于创建 PipelineNode 的字段 dict 列表。"""
    if not snapshot:
        return []
    return [
        {
            "node_type": n.get("node_type"),
            "name": n.get("name"),
            "node_index": n.get("node_index"),
            "config": n.get("config") or {},
            "flow_meta": n.get("flow_meta"),
        }
        for n in snapshot.get("nodes") or []
    ]
```

- [ ] **Step 4: 运行，确认通过**

Run: `pytest server/tests/test_pipeline_logic.py -q`
Expected: PASS（全部）

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/pipelines/snapshot.py server/tests/test_pipeline_logic.py
git commit -m "test(pipelines): snapshot codec round-trip"
```

---

### Task 3: 节点注册表

**Files:**
- Create: `server/app/modules/pipelines/nodes/__init__.py`
- Create: `server/app/modules/pipelines/nodes/base.py`
- Test: `server/tests/test_pipeline_logic.py`（追加）

- [ ] **Step 1: 追加失败测试**

```python
# 追加到 server/tests/test_pipeline_logic.py
import pytest
from server.app.modules.pipelines.nodes import base as node_base
from server.app.shared.errors import ValidationError


def test_registry_register_and_get():
    node_base.register("dummy", lambda ctx: node_base.NodeResult(output={"ok": 1}, article_ids=[]))
    handler = node_base.get_handler("dummy")
    res = handler(None)
    assert res.output == {"ok": 1}


def test_registry_unknown_type_raises():
    with pytest.raises(ValidationError):
        node_base.get_handler("nope-does-not-exist")
```

- [ ] **Step 2: 运行，确认失败**

Run: `pytest server/tests/test_pipeline_logic.py -q -k registry`
Expected: FAIL

- [ ] **Step 3: 实现 base + 包 init**

```python
# server/app/modules/pipelines/nodes/base.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from server.app.shared.errors import ValidationError


@dataclass
class NodeRunContext:
    session_factory: Callable[[], Any]
    user_id: int
    config: dict
    inputs: dict           # 经 flow_meta inputMapping 注入
    upstream: dict         # 上游累积 context（node_index -> output 的合并视图）


@dataclass
class NodeResult:
    output: dict = field(default_factory=dict)
    article_ids: list[int] = field(default_factory=list)


NodeHandler = Callable[[NodeRunContext], NodeResult]
_REGISTRY: dict[str, NodeHandler] = {}


def register(node_type: str, handler: NodeHandler) -> None:
    _REGISTRY[node_type] = handler


def get_handler(node_type: str) -> NodeHandler:
    handler = _REGISTRY.get(node_type)
    if handler is None:
        raise ValidationError(f"未知节点类型: {node_type}")
    return handler


def registered_types() -> list[str]:
    return sorted(_REGISTRY.keys())
```

```python
# server/app/modules/pipelines/nodes/__init__.py
# 触发内置节点注册（Task 5 填充）
from server.app.modules.pipelines.nodes import base  # noqa: F401
```

- [ ] **Step 4: 运行，确认通过**

Run: `pytest server/tests/test_pipeline_logic.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/pipelines/nodes/
git commit -m "test(pipelines): node registry"
```

---

## Phase 2 — 后端模型与迁移

### Task 4: SQLAlchemy 模型 + Alembic 迁移

**Files:**
- Create: `server/app/modules/pipelines/models.py`
- Create: `server/alembic/versions/0037_pipelines.py`（若 head 已变，改用实际 head+1 命名）

- [ ] **Step 1: 模型**

```python
# server/app/modules/pipelines/models.py
from datetime import datetime

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from server.app.core.time import utcnow
from server.app.db.base import Base


class Pipeline(Base):
    __tablename__ = "pipelines"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    has_draft: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class PipelineNode(Base):
    __tablename__ = "pipeline_nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"), index=True)
    node_type: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    node_index: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    flow_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PipelineVersion(Base):
    __tablename__ = "pipeline_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending','running','done','partial_failed','failed')",
            name="ck_pipeline_runs_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    node_results: Mapped[dict] = mapped_column(JSON, default=dict)
    article_ids: Mapped[list] = mapped_column(JSON, default=list)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
```

- [ ] **Step 2: 确认当前 head**

Run: `ls server/alembic/versions/ | sort | tail -3`
Expected: 最新为 `0036_scheme_ai_engine.py`。若不是，下一步 revision/down_revision 用实际最新值，文件名顺延。

- [ ] **Step 3: 迁移**

```python
# server/alembic/versions/0037_pipelines.py
"""pipelines orchestration tables

Revision ID: 0037
Revises: 0036
Create Date: 2026-06-04
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pipelines",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("draft_snapshot", sa.JSON(), nullable=True),
        sa.Column("has_draft", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_pipelines_user_id"), "pipelines", ["user_id"])

    op.create_table(
        "pipeline_nodes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pipeline_id", sa.Integer(), nullable=False),
        sa.Column("node_type", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("node_index", sa.Integer(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("flow_meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_pipeline_nodes_pipeline_id"), "pipeline_nodes", ["pipeline_id"])

    op.create_table(
        "pipeline_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pipeline_id", sa.Integer(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("remark", sa.String(length=255), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pipeline_versions_pipeline_version",
        "pipeline_versions", ["pipeline_id", "version_no"],
    )

    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pipeline_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("node_results", sa.JSON(), nullable=True),
        sa.Column("article_ids", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status in ('pending','running','done','partial_failed','failed')",
            name="ck_pipeline_runs_status",
        ),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_pipeline_runs_pipeline_id"), "pipeline_runs", ["pipeline_id"])
    op.create_index(op.f("ix_pipeline_runs_user_id"), "pipeline_runs", ["user_id"])
    op.create_index(op.f("ix_pipeline_runs_status"), "pipeline_runs", ["status"])


def downgrade() -> None:
    op.drop_table("pipeline_runs")
    op.drop_table("pipeline_versions")
    op.drop_table("pipeline_nodes")
    op.drop_table("pipelines")
```

- [ ] **Step 4: 应用迁移验证**

Run: `alembic upgrade head`
Expected: 无报错；`alembic current` 显示 `0037`。

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/pipelines/models.py server/alembic/versions/0037_pipelines.py
git commit -m "feat(pipelines): models + alembic migration"
```

---

## Phase 3 — 内置节点

### Task 5: `input` 与 `ai_generate` 节点

**Files:**
- Create: `server/app/modules/pipelines/nodes/input_node.py`
- Create: `server/app/modules/pipelines/nodes/ai_generate_node.py`
- Modify: `server/app/modules/pipelines/nodes/__init__.py`
- Test: `server/tests/test_pipeline_logic.py`（追加 input 节点纯逻辑测试）

- [ ] **Step 1: 追加 input 节点失败测试**

```python
# 追加到 server/tests/test_pipeline_logic.py
def test_input_node_outputs_question_text():
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.input_node import run_input

    ctx = NodeRunContext(session_factory=None, user_id=1,
                         config={"question_text": "今天写什么"}, inputs={}, upstream={})
    res = run_input(ctx)
    assert res.output == {"question_text": "今天写什么"}
    assert res.article_ids == []
```

- [ ] **Step 2: 运行，确认失败**

Run: `pytest server/tests/test_pipeline_logic.py -q -k input_node`
Expected: FAIL

- [ ] **Step 3: 实现 input 节点**

```python
# server/app/modules/pipelines/nodes/input_node.py
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register


def run_input(ctx: NodeRunContext) -> NodeResult:
    text = (ctx.config or {}).get("question_text", "")
    return NodeResult(output={"question_text": text}, article_ids=[])


register("input", run_input)
```

- [ ] **Step 4: 实现 ai_generate 节点**

```python
# server/app/modules/pipelines/nodes/ai_generate_node.py
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError


def run_ai_generate(ctx: NodeRunContext) -> NodeResult:
    from server.app.modules.ai_generation.article_writer import generate_article_from_prompt
    from server.app.modules.prompt_templates.service import get_prompt_template

    cfg = ctx.config or {}
    # question_text：优先来自上游注入，其次 config 兜底
    question_text = ctx.inputs.get("question_text") or cfg.get("question_text") or ""
    if not question_text:
        raise ValidationError("ai_generate 节点缺少 question_text（上游未传且未配置）")

    template_id = cfg.get("prompt_template_id")
    count = int(cfg.get("count") or 0)
    model = cfg.get("model")
    if not template_id or count <= 0:
        raise ValidationError("ai_generate 节点需配置 prompt_template_id 与 count>0")

    db = ctx.session_factory()
    try:
        tpl = get_prompt_template(db, template_id)
        if tpl is None or tpl.scope != "generation" or not tpl.is_enabled or tpl.is_deleted:
            raise ValidationError("提示词模板无效（不存在/停用/删除/非 generation）")
        template_content = tpl.content
    finally:
        db.close()

    article_ids: list[int] = []
    errors: list[str] = []
    for _ in range(count):
        try:
            aid = generate_article_from_prompt(
                session_factory=ctx.session_factory,
                user_id=ctx.user_id,
                template_content=template_content,
                question_text=question_text,
                model=model,
            )
            article_ids.append(aid)
        except Exception as exc:  # 单篇失败不中断，交由 run 聚合 partial_failed
            errors.append(str(exc))

    return NodeResult(
        output={"article_ids": article_ids, "errors": errors},
        article_ids=article_ids,
    )


register("ai_generate", run_ai_generate)
```

- [ ] **Step 5: 注册（更新 nodes/__init__.py）**

```python
# server/app/modules/pipelines/nodes/__init__.py
from server.app.modules.pipelines.nodes import base  # noqa: F401
from server.app.modules.pipelines.nodes import ai_generate_node  # noqa: F401
from server.app.modules.pipelines.nodes import input_node  # noqa: F401
```

- [ ] **Step 6: 运行单测，确认通过**

Run: `pytest server/tests/test_pipeline_logic.py -q`
Expected: PASS（含 input_node，且 import nodes 包能注册 input/ai_generate 不报错）

- [ ] **Step 7: 提交**

```bash
git add server/app/modules/pipelines/nodes/
git commit -m "feat(pipelines): input and ai_generate built-in nodes"
```

---

## Phase 4 — 执行器、服务、路由

### Task 6: schemas + CRUD service

**Files:**
- Create: `server/app/modules/pipelines/schemas.py`
- Create: `server/app/modules/pipelines/service.py`

- [ ] **Step 1: schemas**

```python
# server/app/modules/pipelines/schemas.py
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PipelineCreate(BaseModel):
    name: str
    description: str | None = None


class PipelinePatch(BaseModel):
    name: str | None = None
    description: str | None = None


class NodeRead(BaseModel):
    node_type: str
    name: str
    node_index: int
    config: dict
    flow_meta: dict | None = None


class PipelineRead(BaseModel):
    id: int
    name: str
    description: str | None
    has_draft: bool
    created_at: datetime
    updated_at: datetime
    nodes: list[NodeRead] = []
    model_config = ConfigDict(from_attributes=True)


class DraftSave(BaseModel):
    snapshot: dict


class PublishRequest(BaseModel):
    remark: str | None = None


class VersionRead(BaseModel):
    id: int
    pipeline_id: int
    version_no: int
    remark: str | None
    created_by: int
    created_at: datetime
    snapshot: dict | None = None
    model_config = ConfigDict(from_attributes=True)


class RunRead(BaseModel):
    id: int
    pipeline_id: int
    status: str
    article_ids: list = []
    node_results: dict = {}
    error_message: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    model_config = ConfigDict(from_attributes=True)
```

- [ ] **Step 2: CRUD service**

```python
# server/app/modules/pipelines/service.py
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.app.modules.pipelines.models import (
    Pipeline, PipelineNode, PipelineRun, PipelineVersion,
)
from server.app.modules.pipelines.snapshot import nodes_to_snapshot, snapshot_to_node_dicts
from server.app.shared.errors import ClientError, ValidationError


def create_pipeline(db: Session, *, user_id: int, name: str, description: str | None) -> Pipeline:
    if not name or not name.strip():
        raise ValidationError("名称不能为空")
    p = Pipeline(user_id=user_id, name=name.strip(), description=description, has_draft=False)
    db.add(p)
    db.flush()
    return p


def get_pipeline(db: Session, pipeline_id: int) -> Pipeline | None:
    return db.get(Pipeline, pipeline_id)


def list_pipelines(db: Session, *, user_id: int, is_admin: bool) -> list[Pipeline]:
    q = select(Pipeline).order_by(Pipeline.id.desc())
    if not is_admin:
        q = q.where(Pipeline.user_id == user_id)
    return list(db.execute(q).scalars().all())


def list_nodes(db: Session, pipeline_id: int) -> list[PipelineNode]:
    q = (
        select(PipelineNode)
        .where(PipelineNode.pipeline_id == pipeline_id)
        .order_by(PipelineNode.node_index.asc())
    )
    return list(db.execute(q).scalars().all())


def patch_pipeline(db: Session, p: Pipeline, *, name: str | None, description: str | None) -> Pipeline:
    if name is not None:
        if not name.strip():
            raise ValidationError("名称不能为空")
        p.name = name.strip()
    if description is not None:
        p.description = description
    db.flush()
    return p


def delete_pipeline(db: Session, p: Pipeline) -> None:
    db.query(PipelineNode).filter(PipelineNode.pipeline_id == p.id).delete()
    db.query(PipelineVersion).filter(PipelineVersion.pipeline_id == p.id).delete()
    db.query(PipelineRun).filter(PipelineRun.pipeline_id == p.id).delete()
    db.delete(p)
    db.flush()


def save_draft(db: Session, p: Pipeline, snapshot: dict) -> None:
    p.draft_snapshot = snapshot
    p.has_draft = True
    db.flush()


def discard_draft(db: Session, p: Pipeline) -> None:
    p.draft_snapshot = None
    p.has_draft = False
    db.flush()


def publish_draft(db: Session, p: Pipeline, *, remark: str | None, user_id: int) -> int:
    if not p.has_draft or not p.draft_snapshot:
        raise ClientError("没有可发布的草稿")
    node_dicts = snapshot_to_node_dicts(p.draft_snapshot)
    if not node_dicts:
        raise ClientError("草稿内容为空")
    # 重建 live 节点
    db.query(PipelineNode).filter(PipelineNode.pipeline_id == p.id).delete()
    for nd in node_dicts:
        db.add(PipelineNode(
            pipeline_id=p.id,
            node_type=nd["node_type"],
            name=nd["name"],
            node_index=nd["node_index"],
            config=nd.get("config") or {},
            flow_meta=nd.get("flow_meta"),
        ))
    db.flush()
    # 写版本快照（用 live 节点规范化）
    live = list_nodes(db, p.id)
    next_no = _next_version_no(db, p.id)
    db.add(PipelineVersion(
        pipeline_id=p.id, version_no=next_no,
        snapshot=nodes_to_snapshot(live), remark=remark, created_by=user_id,
    ))
    p.draft_snapshot = None
    p.has_draft = False
    db.flush()
    return next_no


def _next_version_no(db: Session, pipeline_id: int) -> int:
    rows = db.execute(
        select(PipelineVersion.version_no).where(PipelineVersion.pipeline_id == pipeline_id)
    ).scalars().all()
    return (max(rows) if rows else 0) + 1


def list_versions(db: Session, pipeline_id: int) -> list[PipelineVersion]:
    q = (
        select(PipelineVersion)
        .where(PipelineVersion.pipeline_id == pipeline_id)
        .order_by(PipelineVersion.version_no.desc())
    )
    return list(db.execute(q).scalars().all())


def get_version(db: Session, version_id: int) -> PipelineVersion | None:
    return db.get(PipelineVersion, version_id)


def rollback_to_draft(db: Session, p: Pipeline, version: PipelineVersion) -> None:
    p.draft_snapshot = version.snapshot
    p.has_draft = True
    db.flush()
```

- [ ] **Step 3: 编译/导入验证**

Run: `python -c "import server.app.modules.pipelines.service"`
Expected: 无 ImportError

- [ ] **Step 4: 提交**

```bash
git add server/app/modules/pipelines/schemas.py server/app/modules/pipelines/service.py
git commit -m "feat(pipelines): schemas + crud/draft/version service"
```

---

### Task 7: 执行器

**Files:**
- Create: `server/app/modules/pipelines/executor.py`

- [ ] **Step 1: 实现执行器**

```python
# server/app/modules/pipelines/executor.py
from __future__ import annotations

import logging
from typing import Any, Callable

from server.app.core.time import utcnow
from server.app.modules.pipelines.flow_meta import apply_input_mapping, should_skip
from server.app.modules.pipelines.models import PipelineNode, PipelineRun
from server.app.modules.pipelines.nodes.base import NodeRunContext, get_handler

logger = logging.getLogger(__name__)
SessionFactory = Callable[[], Any]


def create_run(db, *, pipeline_id: int, user_id: int) -> PipelineRun:
    run = PipelineRun(
        pipeline_id=pipeline_id, user_id=user_id, status="pending",
        node_results={}, article_ids=[],
    )
    db.add(run)
    db.flush()
    return run


def run_pipeline(run_id: int, session_factory: SessionFactory) -> None:
    """后台线程入口：线性执行节点，聚合 run 状态。"""
    db = session_factory()
    try:
        run = db.get(PipelineRun, run_id)
        if run is None:
            logger.error("run_pipeline: run %s not found", run_id)
            return
        run.status = "running"
        pipeline_id, user_id = run.pipeline_id, run.user_id
        nodes = (
            db.query(PipelineNode)
            .filter(PipelineNode.pipeline_id == pipeline_id)
            .order_by(PipelineNode.node_index.asc())
            .all()
        )
        node_specs = [
            {"node_type": n.node_type, "node_index": n.node_index,
             "config": n.config or {}, "flow_meta": n.flow_meta}
            for n in nodes
        ]
        db.commit()
    finally:
        db.close()

    context: dict[int, dict] = {}      # node_index -> output
    node_results: dict[str, Any] = {}
    article_ids: list[int] = []
    had_success = False
    had_failure = False

    for spec in node_specs:
        idx = spec["node_index"]
        meta = spec["flow_meta"]
        # 上游视图：按 dependsOnIndex 取指定节点输出，否则合并全部已执行输出
        if meta and meta.get("dependsOnIndex") is not None:
            upstream = context.get(meta["dependsOnIndex"], {})
        else:
            upstream = {k: v for out in context.values() for k, v in out.items()}

        if should_skip(meta, upstream):
            node_results[str(idx)] = {"skipped": True}
            continue

        inputs = apply_input_mapping(meta, upstream)
        try:
            handler = get_handler(spec["node_type"])
            result = handler(NodeRunContext(
                session_factory=session_factory, user_id=user_id,
                config=spec["config"], inputs=inputs, upstream=upstream,
            ))
            context[idx] = result.output
            node_results[str(idx)] = result.output
            article_ids.extend(result.article_ids)
            # ai_generate 节点内单篇失败也算部分失败
            if result.output.get("errors"):
                had_failure = True
            if result.article_ids or spec["node_type"] == "input":
                had_success = True
        except Exception as exc:
            logger.exception("pipeline run %s node #%s failed", run_id, idx)
            node_results[str(idx)] = {"error": str(exc)}
            had_failure = True

    # 聚合状态
    if had_failure and had_success:
        status = "partial_failed"
    elif had_failure:
        status = "failed"
    else:
        status = "done"

    db = session_factory()
    try:
        run = db.get(PipelineRun, run_id)
        if run is not None:
            run.status = status
            run.node_results = node_results
            run.article_ids = article_ids
            run.completed_at = utcnow()
            db.commit()
    finally:
        db.close()
```

- [ ] **Step 2: 导入验证**

Run: `python -c "import server.app.modules.pipelines.executor"`
Expected: 无 ImportError

- [ ] **Step 3: 提交**

```bash
git add server/app/modules/pipelines/executor.py
git commit -m "feat(pipelines): linear run executor with data-passing"
```

---

### Task 8: 路由 + 注册到 create_app

**Files:**
- Create: `server/app/modules/pipelines/router.py`
- Modify: `server/app/main.py`

- [ ] **Step 1: router**

```python
# server/app/modules/pipelines/router.py
from __future__ import annotations

import threading
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from server.app.core.security import get_current_user
from server.app.db.session import get_db
from server.app.modules.pipelines import service as svc
from server.app.modules.pipelines.nodes.base import registered_types
from server.app.modules.pipelines.schemas import (
    DraftSave, PipelineCreate, PipelinePatch, PipelineRead, PublishRequest, RunRead, VersionRead,
)
from server.app.modules.system.models import User

router = APIRouter()

# 由 create_app() 注入（后台线程用）
bg_session_factory: Callable[[], Any] | None = None


def _owned(db: Session, pipeline_id: int, user: User):
    p = svc.get_pipeline(db, pipeline_id)
    if p is None or (user.role != "admin" and p.user_id != user.id):
        raise HTTPException(status_code=404, detail="工作流不存在")
    return p


def _to_read(db: Session, p) -> dict:
    nodes = svc.list_nodes(db, p.id)
    data = PipelineRead.model_validate(p).model_dump()
    data["nodes"] = [
        {"node_type": n.node_type, "name": n.name, "node_index": n.node_index,
         "config": n.config or {}, "flow_meta": n.flow_meta}
        for n in nodes
    ]
    return data


@router.get("/node-types")
def get_node_types() -> dict:
    # 节点 config 字段 schema，供前端属性面板渲染
    return {
        "node_types": [
            {"type": "input", "label": "输入源",
             "config_schema": [{"key": "question_text", "type": "textarea", "label": "问题/主题"}]},
            {"type": "ai_generate", "label": "AI 生文",
             "config_schema": [
                 {"key": "prompt_template_id", "type": "prompt_template", "label": "提示词模板"},
                 {"key": "count", "type": "number", "label": "生成数量"},
                 {"key": "model", "type": "text", "label": "模型(可空)"},
             ]},
        ],
        "registered": registered_types(),
    }


@router.get("")
def list_pipelines(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    items = svc.list_pipelines(db, user_id=user.id, is_admin=user.role == "admin")
    return [_to_read(db, p) for p in items]


@router.post("", status_code=201)
def create_pipeline(payload: PipelineCreate, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    p = svc.create_pipeline(db, user_id=user.id, name=payload.name, description=payload.description)
    db.commit()
    return _to_read(db, p)


@router.get("/{pipeline_id}")
def get_pipeline(pipeline_id: int, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    p = _owned(db, pipeline_id, user)
    return _to_read(db, p)


@router.patch("/{pipeline_id}")
def patch_pipeline(pipeline_id: int, payload: PipelinePatch, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    p = _owned(db, pipeline_id, user)
    svc.patch_pipeline(db, p, name=payload.name, description=payload.description)
    db.commit()
    return _to_read(db, p)


@router.delete("/{pipeline_id}", status_code=204)
def delete_pipeline(pipeline_id: int, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    p = _owned(db, pipeline_id, user)
    svc.delete_pipeline(db, p)
    db.commit()


@router.post("/{pipeline_id}/draft")
def save_draft(pipeline_id: int, payload: DraftSave, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    p = _owned(db, pipeline_id, user)
    svc.save_draft(db, p, payload.snapshot)
    db.commit()
    return {"ok": True}


@router.post("/{pipeline_id}/publish")
def publish(pipeline_id: int, payload: PublishRequest, db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    p = _owned(db, pipeline_id, user)
    version_no = svc.publish_draft(db, p, remark=payload.remark, user_id=user.id)
    db.commit()
    return {"version_no": version_no}


@router.post("/{pipeline_id}/draft/discard")
def discard(pipeline_id: int, db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    p = _owned(db, pipeline_id, user)
    svc.discard_draft(db, p)
    db.commit()
    return {"ok": True}


@router.get("/{pipeline_id}/versions")
def list_versions(pipeline_id: int, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    _owned(db, pipeline_id, user)
    out = []
    for v in svc.list_versions(db, pipeline_id):
        vo = VersionRead.model_validate(v).model_dump()
        vo["snapshot"] = None
        out.append(vo)
    return out


@router.get("/versions/{version_id}")
def get_version(version_id: int, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    v = svc.get_version(db, version_id)
    if v is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    _owned(db, v.pipeline_id, user)
    return VersionRead.model_validate(v).model_dump()


@router.post("/versions/{version_id}/rollback")
def rollback(version_id: int, db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    v = svc.get_version(db, version_id)
    if v is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    p = _owned(db, v.pipeline_id, user)
    svc.rollback_to_draft(db, p, v)
    db.commit()
    return {"ok": True}


@router.post("/{pipeline_id}/runs", status_code=202)
def create_run(pipeline_id: int, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)) -> JSONResponse:
    from server.app.modules.pipelines.executor import create_run as _create_run
    from server.app.modules.pipelines.executor import run_pipeline

    p = _owned(db, pipeline_id, user)
    if not svc.list_nodes(db, p.id):
        raise HTTPException(status_code=400, detail="工作流没有已发布的节点，请先发布")
    run = _create_run(db, pipeline_id=p.id, user_id=user.id)
    db.commit()
    run_id = run.id
    factory = bg_session_factory
    threading.Thread(target=run_pipeline, args=(run_id, factory), daemon=True).start()
    return JSONResponse(status_code=202, content={"run_id": run_id, "status": "pending"})


@router.get("/{pipeline_id}/runs")
def list_runs(pipeline_id: int, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    from server.app.modules.pipelines.models import PipelineRun
    _owned(db, pipeline_id, user)
    rows = (db.query(PipelineRun).filter(PipelineRun.pipeline_id == pipeline_id)
            .order_by(PipelineRun.id.desc()).all())
    return [RunRead.model_validate(r).model_dump() for r in rows]


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    from server.app.modules.pipelines.models import PipelineRun
    r = db.get(PipelineRun, run_id)
    if r is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    _owned(db, r.pipeline_id, user)
    return RunRead.model_validate(r).model_dump()
```

> 路由注册顺序注意：FastAPI 中 `/versions/{version_id}` 与 `/{pipeline_id}/...` 不冲突（前缀不同段）。`/runs/{run_id}` 同理。无需特殊排序。

- [ ] **Step 2: 注册到 main.py**

先 `grep -n "scheme_router\|bg_session_factory\|include_router" server/app/main.py` 定位现有注册块。仿照 scheme_router 加：

import 区：
```python
from server.app.modules.pipelines.router import router as pipelines_router
import server.app.modules.pipelines.nodes  # noqa: F401  触发节点注册
```
include 区（在其它 include_router 附近）：
```python
app.include_router(
    pipelines_router,
    prefix="/api/pipelines",
    tags=["pipelines"],
    dependencies=[Depends(get_current_user)],
)
```
bg_session_factory 注入区（紧随 scheme_router 注入处）：
```python
import server.app.modules.pipelines.router as _pipelines_routes
_pipelines_routes.bg_session_factory = SessionLocal
```

- [ ] **Step 3: 启动导入验证**

Run: `python -c "from server.app.main import create_app; create_app()"`
（需设置最小环境变量 `GEO_JWT_SECRET` / `GEO_DATA_DIR` / `GEO_DATABASE_URL`，见 CLAUDE.md。）
Expected: 无异常，路由挂载成功。

- [ ] **Step 4: 提交**

```bash
git add server/app/modules/pipelines/router.py server/app/main.py
git commit -m "feat(pipelines): router + app registration + bg injection"
```

---

### Task 9: 集成测试（@pytest.mark.mysql，端到端，monkeypatch LLM）

**Files:**
- Create: `server/tests/test_pipelines_api.py`

- [ ] **Step 1: 写测试**

```python
# server/tests/test_pipelines_api.py
import pytest

from server.tests.utils import build_test_app


def _create_generation_template(client) -> int:
    resp = client.post("/api/prompt-templates", json={
        "name": "测试模板", "content": "写一篇关于：", "scope": "generation",
    })
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


@pytest.mark.mysql
def test_pipeline_draft_publish_version_and_run(monkeypatch):
    # monkeypatch 掉真实 LLM 调用
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_generate_node.generate_article_from_prompt",
        lambda **kwargs: 12345,
    )
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        tpl_id = _create_generation_template(client)

        # 1) 新建 pipeline
        r = client.post("/api/pipelines", json={"name": "我的流程"})
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        assert r.json()["has_draft"] is False

        # 2) 存草稿：input -> ai_generate，inputMapping 传 question_text
        snapshot = {"schemaVersion": 1, "nodes": [
            {"node_type": "input", "name": "源", "node_index": 0,
             "config": {"question_text": "如何养生"}, "flow_meta": None},
            {"node_type": "ai_generate", "name": "生文", "node_index": 1,
             "config": {"prompt_template_id": tpl_id, "count": 2},
             "flow_meta": {"schemaVersion": 1,
                           "inputMapping": [{"from": "question_text", "to": "question_text"}]}},
        ]}
        r = client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snapshot})
        assert r.status_code == 200, r.text

        # 草稿不影响 live：此时无已发布节点 -> 运行应 400
        r = client.post(f"/api/pipelines/{pid}/runs")
        assert r.status_code == 400

        # 3) 发布 -> 版本号 1，live 节点出现
        r = client.post(f"/api/pipelines/{pid}/publish", json={"remark": "v1"})
        assert r.status_code == 200, r.text
        assert r.json()["version_no"] == 1
        detail = client.get(f"/api/pipelines/{pid}").json()
        assert detail["has_draft"] is False
        assert len(detail["nodes"]) == 2

        # 4) 版本列表
        vers = client.get(f"/api/pipelines/{pid}/versions").json()
        assert len(vers) == 1 and vers[0]["version_no"] == 1

        # 5) 运行（测试内同步执行）
        from server.app.modules.pipelines.executor import create_run, run_pipeline
        with test_app.session_factory() as db:
            from server.app.modules.pipelines.models import Pipeline
            p = db.get(Pipeline, pid)
            run = create_run(db, pipeline_id=p.id, user_id=p.user_id)
            db.commit()
            run_id = run.id
        run_pipeline(run_id, test_app.session_factory)

        run = client.get(f"/api/pipelines/runs/{run_id}").json()
        assert run["status"] == "done", run
        assert run["article_ids"] == [12345, 12345]

        # 6) 回溯：先再发布一版以制造历史，再回溯 v1 到草稿
        r = client.post(f"/api/pipelines/{pid}/publish", json={"remark": "v2"})
        # 注意：publish 需要 has_draft；此处先存一次草稿再发布
        # （上一次 publish 已清空草稿，因此这里应 400）
        assert r.status_code == 400

        v1_id = vers[0]["id"]
        r = client.post(f"/api/pipelines/versions/{v1_id}/rollback")
        assert r.status_code == 200
        assert client.get(f"/api/pipelines/{pid}").json()["has_draft"] is True
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_pipeline_skip_condition(monkeypatch):
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_generate_node.generate_article_from_prompt",
        lambda **kwargs: 999,
    )
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        tpl_id = _create_generation_template(client)
        pid = client.post("/api/pipelines", json={"name": "条件流程"}).json()["id"]
        snapshot = {"schemaVersion": 1, "nodes": [
            {"node_type": "input", "name": "源", "node_index": 0,
             "config": {"question_text": "x"}, "flow_meta": None},
            {"node_type": "ai_generate", "name": "生文", "node_index": 1,
             "config": {"prompt_template_id": tpl_id, "count": 1},
             "flow_meta": {"condition": {"field": "question_text", "op": "eq", "value": "不匹配"}}},
        ]}
        client.post(f"/api/pipelines/{pid}/draft", json={"snapshot": snapshot})
        client.post(f"/api/pipelines/{pid}/publish", json={})

        from server.app.modules.pipelines.executor import create_run, run_pipeline
        from server.app.modules.pipelines.models import Pipeline
        with test_app.session_factory() as db:
            p = db.get(Pipeline, pid)
            run = create_run(db, pipeline_id=p.id, user_id=p.user_id)
            db.commit()
            run_id = run.id
        run_pipeline(run_id, test_app.session_factory)

        run = client.get(f"/api/pipelines/runs/{run_id}").json()
        assert run["article_ids"] == []  # ai_generate 被跳过
        assert run["node_results"]["1"] == {"skipped": True}
    finally:
        test_app.cleanup()
```

> 若 `POST /api/prompt-templates` 的字段/返回与此不符，先 `grep -n "post\|response_model" server/app/modules/prompt_templates/router.py` 校正 `_create_generation_template`。

- [ ] **Step 2: 运行（需 GEO_TEST_DATABASE_URL）**

Run: `GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test pytest server/tests/test_pipelines_api.py -q`
Expected: 2 passed

- [ ] **Step 3: 全后端回归**

Run: `GEO_TEST_DATABASE_URL=... pytest server/tests/ -q` + `ruff check server/` + `mypy server/app`
Expected: 全绿（mypy 宽松）

- [ ] **Step 4: 提交**

```bash
git add server/tests/test_pipelines_api.py
git commit -m "test(pipelines): end-to-end draft/publish/version/run + skip-condition"
```

---

## Phase 5 — 前端

### Task 10: 类型 + API 客户端

**Files:**
- Modify: `web/src/types.ts`
- Create: `web/src/api/pipelines.ts`

- [ ] **Step 1: 类型（追加到 types.ts）**

```typescript
// web/src/types.ts 追加
export interface PipelineNodeDef {
  node_type: string;
  name: string;
  node_index: number;
  config: Record<string, unknown>;
  flow_meta: PipelineFlowMeta | null;
}
export interface PipelineFlowMeta {
  schemaVersion?: number;
  dependsOnIndex?: number | null;
  inputMapping?: { from: string; to: string }[];
  condition?: { field: string; op: "eq" | "neq" | "contains"; value: string } | null;
}
export interface Pipeline {
  id: number;
  name: string;
  description: string | null;
  has_draft: boolean;
  created_at: string;
  updated_at: string;
  nodes: PipelineNodeDef[];
}
export interface PipelineVersionSummary {
  id: number; pipeline_id: number; version_no: number;
  remark: string | null; created_by: number; created_at: string;
}
export interface PipelineRun {
  id: number; pipeline_id: number; status: string;
  article_ids: number[]; node_results: Record<string, unknown>;
  error_message: string | null; created_at: string; completed_at: string | null;
}
export interface NodeTypeDef {
  type: string; label: string;
  config_schema: { key: string; type: string; label: string }[];
}
```

- [ ] **Step 2: API 客户端**

```typescript
// web/src/api/pipelines.ts
import { api } from "./core";
import type { NodeTypeDef, Pipeline, PipelineRun, PipelineVersionSummary } from "../types";

export const listPipelines = () => api<Pipeline[]>("/api/pipelines");
export const getPipeline = (id: number) => api<Pipeline>(`/api/pipelines/${id}`);
export const createPipeline = (p: { name: string; description?: string }) =>
  api<Pipeline>("/api/pipelines", { method: "POST", body: JSON.stringify(p) });
export const patchPipeline = (id: number, p: { name?: string; description?: string }) =>
  api<Pipeline>(`/api/pipelines/${id}`, { method: "PATCH", body: JSON.stringify(p) });
export const deletePipeline = (id: number) =>
  api<void>(`/api/pipelines/${id}`, { method: "DELETE" });

export const getNodeTypes = () =>
  api<{ node_types: NodeTypeDef[]; registered: string[] }>("/api/pipelines/node-types");

export const saveDraft = (id: number, snapshot: unknown) =>
  api<{ ok: boolean }>(`/api/pipelines/${id}/draft`, { method: "POST", body: JSON.stringify({ snapshot }) });
export const publishPipeline = (id: number, remark?: string) =>
  api<{ version_no: number }>(`/api/pipelines/${id}/publish`, { method: "POST", body: JSON.stringify({ remark }) });
export const discardDraft = (id: number) =>
  api<{ ok: boolean }>(`/api/pipelines/${id}/draft/discard`, { method: "POST" });

export const listVersions = (id: number) =>
  api<PipelineVersionSummary[]>(`/api/pipelines/${id}/versions`);
export const rollbackVersion = (versionId: number) =>
  api<{ ok: boolean }>(`/api/pipelines/versions/${versionId}/rollback`, { method: "POST" });

export const startRun = (id: number) =>
  api<{ run_id: number; status: string }>(`/api/pipelines/${id}/runs`, { method: "POST" });
export const getRun = (runId: number) => api<PipelineRun>(`/api/pipelines/runs/${runId}`);
```

- [ ] **Step 3: typecheck**

Run: `pnpm --filter @geo/web typecheck`
Expected: 通过

- [ ] **Step 4: 提交**

```bash
git add web/src/types.ts web/src/api/pipelines.ts
git commit -m "feat(pipelines): frontend types + api client"
```

---

### Task 11: 导航 tab + Workspace 外壳 + pipeline 列表

**Files:**
- Modify: `web/src/types.ts`（NavKey + navItems）
- Modify: `web/src/App.tsx`
- Create: `web/src/features/pipelines/PipelinesWorkspace.tsx`

- [ ] **Step 1: NavKey + navItems**

在 `web/src/types.ts`：`NavKey` union 加 `"pipelines"`；`navItems` 数组**最前面**或 AI 生文之前插入：
```typescript
import { Workflow } from "lucide-react";   // 确认 lucide-react 有 Workflow 图标，没有则用 Network
// navItems 顶部：
{ key: "pipelines", label: "工作流编排", icon: Workflow },
```

- [ ] **Step 2: App.tsx 渲染块**

`grep -n "AiGenerationWorkspace\|visitedTabs\|activeNav === \"ai\"" web/src/App.tsx` 定位现有 tab 块，仿照加：
```tsx
import { PipelinesWorkspace } from "./features/pipelines/PipelinesWorkspace";
// workspace 区：
{visitedTabs.has("pipelines") && (
  <div style={{ display: activeNav === "pipelines" ? undefined : "none" }}>
    <ErrorBoundary fallback={<p role="alert">工作流编排出错，请刷新重试</p>}>
      <PipelinesWorkspace />
    </ErrorBoundary>
  </div>
)}
```

- [ ] **Step 3: Workspace 外壳 + 列表**

```tsx
// web/src/features/pipelines/PipelinesWorkspace.tsx
import { useCallback, useEffect, useState } from "react";
import { createPipeline, deletePipeline, listPipelines } from "../../api/pipelines";
import { useToast } from "../../components/Toast";
import type { Pipeline } from "../../types";
import { PipelineEditor } from "./PipelineEditor";

export function PipelinesWorkspace() {
  const { toast } = useToast();
  const [items, setItems] = useState<Pipeline[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const reload = useCallback(async () => {
    try {
      const list = await listPipelines();
      setItems(list);
      if (selectedId == null && list.length) setSelectedId(list[0].id);
    } catch (e) {
      toast(e instanceof Error ? e.message : "加载失败", "error");
    }
  }, [selectedId, toast]);

  useEffect(() => { reload(); }, [reload]);

  const onCreate = async () => {
    const name = window.prompt("工作流名称");
    if (!name) return;
    const p = await createPipeline({ name });
    await reload();
    setSelectedId(p.id);
  };

  const onDelete = async (id: number) => {
    if (!window.confirm("确认删除该工作流？")) return;
    await deletePipeline(id);
    if (selectedId === id) setSelectedId(null);
    reload();
  };

  return (
    <div className="pipelinesWorkspace">
      <div className="topbar"><div><p className="eyebrow">编排</p><h1>工作流编排</h1></div>
        <button onClick={onCreate}>+ 新建工作流</button></div>
      <div style={{ display: "flex", gap: 16 }}>
        <aside style={{ width: 220 }}>
          {items.map((p) => (
            <div key={p.id} onClick={() => setSelectedId(p.id)}
                 style={{ fontWeight: p.id === selectedId ? 700 : 400, cursor: "pointer", padding: 6 }}>
              {p.name}{p.has_draft ? " ●" : ""}
              <button style={{ float: "right" }} onClick={(e) => { e.stopPropagation(); onDelete(p.id); }}>删</button>
            </div>
          ))}
        </aside>
        <main style={{ flex: 1 }}>
          {selectedId != null
            ? <PipelineEditor pipelineId={selectedId} onChanged={reload} />
            : <p>请选择或新建工作流</p>}
        </main>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: 占位 PipelineEditor（Task 12 完善）**

先创建最小可编译占位，避免本任务 import 失败：
```tsx
// web/src/features/pipelines/PipelineEditor.tsx
export function PipelineEditor({ pipelineId }: { pipelineId: number; onChanged: () => void }) {
  return <div>编辑器占位：pipeline {pipelineId}</div>;
}
```

- [ ] **Step 5: typecheck + build**

Run: `pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 通过；新 tab 出现在导航。

- [ ] **Step 6: 提交**

```bash
git add web/src/types.ts web/src/App.tsx web/src/features/pipelines/
git commit -m "feat(pipelines): nav tab + workspace shell + pipeline list"
```

---

### Task 12: 编辑器（线性节点 + 属性面板 + 数据传递 + 草稿/发布/版本/运行）

**Files:**
- Modify: `web/src/features/pipelines/PipelineEditor.tsx`（替换占位）
- Create: `web/src/features/pipelines/VersionHistory.tsx`

- [ ] **Step 1: VersionHistory**

```tsx
// web/src/features/pipelines/VersionHistory.tsx
import { useEffect, useState } from "react";
import { listVersions, rollbackVersion } from "../../api/pipelines";
import type { PipelineVersionSummary } from "../../types";

export function VersionHistory({ pipelineId, onRolledBack }:
  { pipelineId: number; onRolledBack: () => void }) {
  const [rows, setRows] = useState<PipelineVersionSummary[]>([]);
  useEffect(() => { listVersions(pipelineId).then(setRows).catch(() => {}); }, [pipelineId]);
  return (
    <div>
      <h4>版本历史</h4>
      {rows.map((v) => (
        <div key={v.id}>
          v{v.version_no} {v.remark ?? ""} {new Date(v.created_at).toLocaleString()}
          <button onClick={async () => {
            if (!window.confirm(`回溯到 v${v.version_no}？将载入草稿，需手动发布后才生效`)) return;
            await rollbackVersion(v.id);
            onRolledBack();
          }}>回溯</button>
        </div>
      ))}
      {rows.length === 0 && <p>暂无版本</p>}
    </div>
  );
}
```

- [ ] **Step 2: 编辑器全量实现**

```tsx
// web/src/features/pipelines/PipelineEditor.tsx
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  discardDraft, getNodeTypes, getPipeline, getRun, publishPipeline, saveDraft, startRun,
} from "../../api/pipelines";
import { useToast } from "../../components/Toast";
import type { NodeTypeDef, Pipeline, PipelineNodeDef } from "../../types";
import { VersionHistory } from "./VersionHistory";

export function PipelineEditor({ pipelineId, onChanged }:
  { pipelineId: number; onChanged: () => void }) {
  const { toast } = useToast();
  const [nodes, setNodes] = useState<PipelineNodeDef[]>([]);
  const [hasDraft, setHasDraft] = useState(false);
  const [nodeTypes, setNodeTypes] = useState<NodeTypeDef[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [showVersions, setShowVersions] = useState(false);
  const [runStatus, setRunStatus] = useState<string | null>(null);

  const load = useCallback(async () => {
    const p: Pipeline = await getPipeline(pipelineId);
    setNodes(p.nodes);
    setHasDraft(p.has_draft);
    setSelected(p.nodes.length ? 0 : null);
  }, [pipelineId]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { getNodeTypes().then((r) => setNodeTypes(r.node_types)).catch(() => {}); }, []);

  const reindex = (list: PipelineNodeDef[]) => list.map((n, i) => ({ ...n, node_index: i }));

  const addNode = (type: string) => {
    const def = nodeTypes.find((t) => t.type === type);
    const next = reindex([...nodes, {
      node_type: type, name: def?.label ?? type, node_index: nodes.length,
      config: {}, flow_meta: null,
    }]);
    setNodes(next); setSelected(next.length - 1);
  };
  const removeNode = (i: number) => {
    const next = reindex(nodes.filter((_, idx) => idx !== i));
    setNodes(next); setSelected(next.length ? 0 : null);
  };
  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= nodes.length) return;
    const copy = [...nodes];
    [copy[i], copy[j]] = [copy[j], copy[i]];
    setNodes(reindex(copy)); setSelected(j);
  };
  const updateNode = (i: number, patch: Partial<PipelineNodeDef>) =>
    setNodes(nodes.map((n, idx) => (idx === i ? { ...n, ...patch } : n)));

  const snapshot = useMemo(() => ({ schemaVersion: 1, nodes }), [nodes]);

  const onSaveDraft = async () => {
    await saveDraft(pipelineId, snapshot); setHasDraft(true); onChanged();
    toast("草稿已保存", "success");
  };
  const onPublish = async () => {
    await saveDraft(pipelineId, snapshot);
    const { version_no } = await publishPipeline(pipelineId);
    setHasDraft(false); onChanged(); toast(`已发布 v${version_no}`, "success");
  };
  const onDiscard = async () => {
    if (!window.confirm("丢弃未发布改动？")) return;
    await discardDraft(pipelineId); await load(); onChanged();
  };
  const onRun = async () => {
    try {
      const { run_id } = await startRun(pipelineId);
      setRunStatus("running");
      const poll = setInterval(async () => {
        const r = await getRun(run_id);
        setRunStatus(`${r.status}（文章 ${r.article_ids.length} 篇）`);
        if (["done", "failed", "partial_failed"].includes(r.status)) clearInterval(poll);
      }, 1500);
    } catch (e) {
      toast(e instanceof Error ? e.message : "运行失败", "error");
    }
  };

  const sel = selected != null ? nodes[selected] : null;
  const selDef = sel ? nodeTypes.find((t) => t.type === sel.node_type) : null;

  return (
    <div>
      <div style={{ marginBottom: 8 }}>
        {hasDraft && <span style={{ color: "orange", marginRight: 8 }}>● 有未发布草稿</span>}
        <button onClick={onSaveDraft}>保存草稿</button>
        <button onClick={onPublish}>发布</button>
        <button onClick={onDiscard} disabled={!hasDraft}>丢弃草稿</button>
        <button onClick={() => setShowVersions((v) => !v)}>版本历史</button>
        <button onClick={onRun}>运行</button>
        {runStatus && <span style={{ marginLeft: 8 }}>运行状态：{runStatus}</span>}
      </div>

      <div style={{ marginBottom: 8 }}>
        {nodeTypes.map((t) => (
          <button key={t.type} onClick={() => addNode(t.type)}>+ {t.label}</button>
        ))}
      </div>

      <div style={{ display: "flex", gap: 16 }}>
        {/* 线性节点列表 */}
        <div style={{ width: 240 }}>
          {nodes.map((n, i) => (
            <div key={i} onClick={() => setSelected(i)}
                 style={{ border: i === selected ? "2px solid #06f" : "1px solid #ccc",
                          padding: 8, marginBottom: 6, cursor: "pointer" }}>
              <div>#{n.node_index} {n.name} <em>({n.node_type})</em></div>
              <button onClick={(e) => { e.stopPropagation(); move(i, -1); }}>↑</button>
              <button onClick={(e) => { e.stopPropagation(); move(i, 1); }}>↓</button>
              <button onClick={(e) => { e.stopPropagation(); removeNode(i); }}>删</button>
              {i < nodes.length - 1 && <div style={{ textAlign: "center" }}>↓</div>}
            </div>
          ))}
        </div>

        {/* 属性面板 */}
        <div style={{ flex: 1 }}>
          {sel && selDef ? (
            <div>
              <h4>{sel.name} 配置</h4>
              <label>节点名称
                <input value={sel.name}
                       onChange={(e) => updateNode(selected!, { name: e.target.value })} />
              </label>
              {selDef.config_schema.map((f) => (
                <div key={f.key}>
                  <label>{f.label}
                    {f.type === "textarea"
                      ? <textarea value={String(sel.config[f.key] ?? "")}
                          onChange={(e) => updateNode(selected!,
                            { config: { ...sel.config, [f.key]: e.target.value } })} />
                      : <input type={f.type === "number" ? "number" : "text"}
                          value={String(sel.config[f.key] ?? "")}
                          onChange={(e) => updateNode(selected!,
                            { config: { ...sel.config,
                              [f.key]: f.type === "number" ? Number(e.target.value) : e.target.value } })} />}
                  </label>
                </div>
              ))}

              {/* 数据传递 */}
              <hr /><h5>数据传递</h5>
              <label>上游节点
                <select value={sel.flow_meta?.dependsOnIndex ?? ""}
                  onChange={(e) => updateNode(selected!, { flow_meta: {
                    ...(sel.flow_meta ?? {}),
                    dependsOnIndex: e.target.value === "" ? null : Number(e.target.value),
                  } })}>
                  <option value="">默认（合并全部上游）</option>
                  {nodes.filter((n) => n.node_index < sel.node_index).map((n) => (
                    <option key={n.node_index} value={n.node_index}>#{n.node_index} {n.name}</option>
                  ))}
                </select>
              </label>
              <div>
                <strong>字段映射</strong>
                {(sel.flow_meta?.inputMapping ?? []).map((m, mi) => (
                  <div key={mi}>
                    <input placeholder="上游字段" value={m.from}
                      onChange={(e) => {
                        const im = [...(sel.flow_meta?.inputMapping ?? [])];
                        im[mi] = { ...im[mi], from: e.target.value };
                        updateNode(selected!, { flow_meta: { ...(sel.flow_meta ?? {}), inputMapping: im } });
                      }} />→
                    <input placeholder="本节点字段" value={m.to}
                      onChange={(e) => {
                        const im = [...(sel.flow_meta?.inputMapping ?? [])];
                        im[mi] = { ...im[mi], to: e.target.value };
                        updateNode(selected!, { flow_meta: { ...(sel.flow_meta ?? {}), inputMapping: im } });
                      }} />
                    <button onClick={() => {
                      const im = (sel.flow_meta?.inputMapping ?? []).filter((_, x) => x !== mi);
                      updateNode(selected!, { flow_meta: { ...(sel.flow_meta ?? {}), inputMapping: im } });
                    }}>删</button>
                  </div>
                ))}
                <button onClick={() => {
                  const im = [...(sel.flow_meta?.inputMapping ?? []), { from: "", to: "" }];
                  updateNode(selected!, { flow_meta: { ...(sel.flow_meta ?? {}), inputMapping: im } });
                }}>+ 映射</button>
              </div>
              <div>
                <strong>跳过条件</strong>
                <input placeholder="字段" value={sel.flow_meta?.condition?.field ?? ""}
                  onChange={(e) => updateNode(selected!, { flow_meta: { ...(sel.flow_meta ?? {}),
                    condition: { field: e.target.value,
                      op: sel.flow_meta?.condition?.op ?? "eq",
                      value: sel.flow_meta?.condition?.value ?? "" } } })} />
                <select value={sel.flow_meta?.condition?.op ?? "eq"}
                  onChange={(e) => updateNode(selected!, { flow_meta: { ...(sel.flow_meta ?? {}),
                    condition: { field: sel.flow_meta?.condition?.field ?? "",
                      op: e.target.value as "eq" | "neq" | "contains",
                      value: sel.flow_meta?.condition?.value ?? "" } } })}>
                  <option value="eq">等于</option><option value="neq">不等于</option>
                  <option value="contains">包含</option>
                </select>
                <input placeholder="值" value={sel.flow_meta?.condition?.value ?? ""}
                  onChange={(e) => updateNode(selected!, { flow_meta: { ...(sel.flow_meta ?? {}),
                    condition: { field: sel.flow_meta?.condition?.field ?? "",
                      op: sel.flow_meta?.condition?.op ?? "eq", value: e.target.value } } })} />
              </div>
            </div>
          ) : <p>选择一个节点以编辑</p>}

          {showVersions && (
            <VersionHistory pipelineId={pipelineId}
              onRolledBack={async () => { await load(); onChanged(); setShowVersions(false);
                toast("已载入草稿，请确认后发布", "success"); }} />
          )}
        </div>
      </div>
    </div>
  );
}
```

> 说明：`prompt_template_id` 此处用普通 number 输入；如需下拉选模板，后续可接 `/api/prompt-templates?scope=generation`（非本任务必需）。

- [ ] **Step 3: typecheck + build**

Run: `pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 通过

- [ ] **Step 4: 手动冒烟**

启动后端（`uvicorn server.app.main:app --reload`）+ 前端（`pnpm --filter @geo/web dev`，端口 5173）：新建工作流 → 加 input + ai_generate → 配置 question_text 与映射 → 保存草稿（侧栏出现 ●）→ 发布（提示版本号）→ 运行（状态变 done，文章数>0）→ 版本历史回溯。

- [ ] **Step 5: 提交**

```bash
git add web/src/features/pipelines/
git commit -m "feat(pipelines): editor with node list, property panel, data-passing, draft/version/run"
```

---

## Self-Review 结果

- **Spec 覆盖**：§3 模型→Task 4；§4 节点+注册表→Task 3/5；§5 执行器+数据传递→Task 1/7；§6 草稿/版本/快照→Task 2/6；§7 API→Task 8；§8 前端→Task 10/11/12；§9 验证→Task 1-3/9 单测+集成；§12 验收 1-5 均有对应（草稿不影响 live=Task9 step1 的 400 断言；条件跳过=Task9 第二个测试）。无遗漏。
- **占位符扫描**：无 TBD/TODO；纯逻辑与新文件均给完整代码；对未读文件签名（main.py 注入点、prompt-templates POST 字段、lucide 图标名）给出"先 grep 确认"指令而非假设。
- **类型一致性**：后端 `NodeResult(output, article_ids)` / `NodeRunContext(session_factory,user_id,config,inputs,upstream)` 跨 Task 3/5/7 一致；`flow_meta` 结构（dependsOnIndex/inputMapping/condition）后端 flow_meta.py(Task1)、executor(Task7)、前端类型(Task10)、编辑器(Task12)一致；快照结构 `{schemaVersion,nodes:[{node_type,name,node_index,config,flow_meta}]}` 跨 Task2/6/9/12 一致；API 路径前后端一致（Task8 ↔ Task10）。
- **已知待核对点（执行时 grep 确认，非阻塞）**：main.py 现有注入块行号；`POST /api/prompt-templates` 请求体；`lucide-react` 是否有 `Workflow` 图标；`useToast` 导出路径 `../../components/Toast`。
