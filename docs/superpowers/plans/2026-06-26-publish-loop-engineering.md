# `/publish` 发文 Loop Engineering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `claude-loops/distribute-loop.md` POC 升级成 `/publish <自然语言>` 自然语言入口 + 单 `article_round_robin` task + poll-to-terminal 停止条件的发文 Loop，与 `/goal` 工程化路径对齐。

**Architecture:**
- 后端：扩 `list_articles` 加 `exclude_distributed`（与 pipeline `approved_content_source` 节点同口径）+ 新 `GET /api/tasks/{task_id}/status-mcp` 给主对话 poll
- MCP：扩 `list_articles` 工具签名 + 新 `get_publish_task_status` 工具（catalog 组）
- 模板正本：新增 `publish.md` slash command + `geo-publish-orchestrator/SKILL.md`，走「MCP 接入」tab 分发（`install_loop_skills` MCP 工具 / 前端按钮）

**Tech Stack:** FastAPI / SQLAlchemy / pytest（`pytest.mark.mysql`）/ FastMCP（`@mcp.tool()` 装饰器）/ loop_skills `templates/` + `LOOP_SKILL_BUNDLE_VERSION` bump

**Spec：** [`docs/superpowers/specs/2026-06-26-publish-loop-engineering-design.md`](../specs/2026-06-26-publish-loop-engineering-design.md)

**Worktree：** 实施时建议在新 worktree 里跑（`superpowers:using-git-worktrees`）；不强制。

---

## 文件结构

| 文件 | 操作 | 职责 |
|---|---|---|
| `server/app/modules/articles/service.py` | 修改 | `list_articles` 加 `exclude_distributed: bool = False` 参数，复用 `approved_content_source` 节点判定 |
| `server/app/modules/mcp_catalog/router.py` | 修改 | `mcp_list_articles` 透传新 query 参数 |
| `server/app/modules/tasks/router.py` | 修改 | `tasks_mcp_router` 末尾追加 `GET /{task_id}/status-mcp` endpoint |
| `server/mcp/tools/catalog.py` | 修改 | 扩 `list_articles` 工具签名；新增 `get_publish_task_status` 工具 |
| `server/app/modules/loop_skills/templates/commands/publish.md` | 新建 | `/publish` slash command wrapper |
| `server/app/modules/loop_skills/templates/skills/geo-publish-orchestrator/SKILL.md` | 新建 | orchestrator skill 模板正本 |
| `server/app/modules/loop_skills/version.py` | 修改 | bump `LOOP_SKILL_BUNDLE_VERSION` + 加 CRLF/LF 两个新 sha |
| `server/tests/test_mcp_catalog_articles.py` | 新建 | 4 用例：service 层 + MCP endpoint 行为 |
| `server/tests/test_tasks_status_mcp.py` | 新建 | 5 用例：endpoint 覆盖 |
| `server/tests/test_loop_skill_bundle.py` | 修改 | `test_build_bundle_lists_all_template_files` 预期集合加新 path |

---

## Task 1: `list_articles` 加 `exclude_distributed` 参数（TDD）

**Files:**
- Test: `server/tests/test_mcp_catalog_articles.py`
- Modify: `server/app/modules/articles/service.py`
- Modify: `server/app/modules/mcp_catalog/router.py`

### Step 1.1: 写第一个失败测试（默认 `False` 行为不变）

- [ ] **创建 `server/tests/test_mcp_catalog_articles.py`**：

```python
"""[MCP] /api/mcp/articles `exclude_distributed` 过滤参数。

覆盖：
- 默认 exclude_distributed=False，行为与现状字节一致（防回归）
- exclude_distributed=True 排除「已在 task 在跑」（含 pending/running/succeeded/waiting_*）
- exclude_distributed=True 排除「已 succeeded」（与 published_count 老语义对齐）
- exclude_distributed=True 保留 failed/cancelled/软删 record 对应的文章（允许重发）
"""

from __future__ import annotations

import pytest

from server.app.modules.accounts.models import Account
from server.app.modules.system.models import Platform
from server.app.modules.tasks.models import PublishRecord, PublishTask, PublishTaskAccount


def _tiptap_doc() -> dict:
    return {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "x"}]}],
    }


def _setup_platform_and_account(session) -> tuple[int, int]:
    platform = Platform(code="toutiao", name="头条号", base_url="https://mp.toutiao.com")
    account = Account(
        user_id=1,
        platform=platform,
        display_name="测试账号",
        platform_user_id="test-user",
        status="valid",
        state_path="browser_states/toutiao/test/storage_state.json",
    )
    session.add(platform)
    session.add(account)
    session.flush()
    return platform.id, account.id


def _create_approved_article(client, title: str) -> int:
    """建一篇文章，review_status 默认就是 approved（与 test_mcp_catalog 同约定）。"""
    resp = client.post(
        "/api/articles",
        json={"title": title, "author": "T", "content_json": _tiptap_doc()},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _add_record(
    session, article_id: int, platform_id: int, account_id: int, status: str,
    *, is_deleted: bool = False,
) -> None:
    task = PublishTask(
        user_id=1,
        name="t",
        task_type="single",
        status="succeeded",
        platform_id=platform_id,
        article_id=article_id,
        stop_before_publish=False,
    )
    task.accounts.append(PublishTaskAccount(account_id=account_id, sort_order=0))
    record = PublishRecord(
        task=task,
        article_id=article_id,
        platform_id=platform_id,
        account_id=account_id,
        status=status,
        is_deleted=is_deleted,
    )
    task.records.append(record)
    session.add(task)
    session.flush()


@pytest.mark.mysql
def test_mcp_list_articles_default_does_not_exclude_distributed(monkeypatch):
    """默认 exclude_distributed=False：已 succeeded 的文章仍出现（防回归）。"""
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        a_id = _create_approved_article(test_app.client, "已发布")
        session = test_app.session_factory()
        try:
            pid, acc_id = _setup_platform_and_account(session)
            _add_record(session, a_id, pid, acc_id, "succeeded")
            session.commit()
        finally:
            session.close()

        r = test_app.client.get(
            "/api/mcp/articles?review_status=approved",
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200, r.text
        ids = {item["id"] for item in r.json()}
        assert a_id in ids, "默认不传 exclude_distributed 时已发布文章必须仍出现（防回归）"
    finally:
        test_app.cleanup()
```

### Step 1.2: 跑这个 test，确认它 pass（基线 / 防回归）

这是一个**基线用例**——验证现行 `list_articles` 行为未变。它本来就该过；改完 Step 1.5 / 1.6 之后再跑一次确认依然过 = 防回归。

```bash
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test \
  pytest server/tests/test_mcp_catalog_articles.py::test_mcp_list_articles_default_does_not_exclude_distributed -v
```

Expected: PASS（如果 fail 说明现行 `list_articles` 已有问题，先排查再继续）。

### Step 1.3: 追加第二个测试（`exclude_distributed=True` 排除在途）

- [ ] 在 `test_mcp_catalog_articles.py` 末尾追加：

```python
@pytest.mark.mysql
def test_mcp_list_articles_exclude_distributed_filters_running_records(monkeypatch):
    """exclude_distributed=True：article 有 pending/running/succeeded/waiting_* record 都该被排除。

    与 pipeline approved_content_source 节点同口径：PublishRecord.status NOT IN
    ('failed', 'cancelled') 且未软删 = 已分发/在途，不允许重新分发。
    """
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        a_running = _create_approved_article(test_app.client, "在跑")
        a_succeeded = _create_approved_article(test_app.client, "已发")
        a_waiting = _create_approved_article(test_app.client, "等人确认")
        a_clean = _create_approved_article(test_app.client, "干净候选")

        session = test_app.session_factory()
        try:
            pid, acc_id = _setup_platform_and_account(session)
            _add_record(session, a_running, pid, acc_id, "running")
            _add_record(session, a_succeeded, pid, acc_id, "succeeded")
            _add_record(session, a_waiting, pid, acc_id, "waiting_manual_publish")
            session.commit()
        finally:
            session.close()

        r = test_app.client.get(
            "/api/mcp/articles?review_status=approved&exclude_distributed=true",
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200, r.text
        ids = {item["id"] for item in r.json()}
        assert a_clean in ids, "干净候选必须出现"
        assert a_running not in ids, "在跑文章应被排除"
        assert a_succeeded not in ids, "已发布文章应被排除"
        assert a_waiting not in ids, "等人确认文章应被排除"
    finally:
        test_app.cleanup()
```

### Step 1.4: 跑这个 test，确认它 fail

```bash
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test \
  pytest server/tests/test_mcp_catalog_articles.py::test_mcp_list_articles_exclude_distributed_filters_running_records -v
```

Expected: FAIL（query 参数 `exclude_distributed=true` 现在被忽略，`a_running`/`a_succeeded`/`a_waiting` 会出现）。

### Step 1.5: 改 `server/app/modules/articles/service.py:list_articles` 加参数

- [ ] 打开 `server/app/modules/articles/service.py`，定位第 112 行的 `def list_articles(...)`，签名改成：

```python
def list_articles(
    db: Session,
    query: str | None = None,
    skip: int = 0,
    limit: int = 50,
    user_id: int | None = None,
    review_status: str | None = None,
    exclude_distributed: bool = False,
) -> list[Article]:
```

- [ ] 在 `stmt = ( select(Article) ... )` 后、`stmt.offset(skip).limit(limit)` **之前**插入：

```python
    if exclude_distributed:
        # 与 pipeline approved_content_source 节点同口径（CLAUDE.md 明文）：
        # 「已分发或在途」= 存在 PublishRecord.status NOT IN ('failed', 'cancelled') 且未软删。
        # failed/cancelled/软删的记录允许重新分发（可重试，不永久埋没）。
        distributed = select(PublishRecord.article_id).where(
            PublishRecord.is_deleted == False,  # noqa: E712
            PublishRecord.status.notin_(["failed", "cancelled"]),
        )
        stmt = stmt.where(Article.id.notin_(distributed))
```

注意：`PublishRecord` 已经在文件顶部 import 过（第 41 行）。

### Step 1.6: 改 `mcp_list_articles` 透传参数

- [ ] 打开 `server/app/modules/mcp_catalog/router.py`，把 `mcp_list_articles` 签名改成：

```python
@router.get("/articles", response_model=list[ArticleListRead])
def mcp_list_articles(
    status: str | None = Query(default=None),
    review_status: str | None = Query(default=None),
    exclude_distributed: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[ArticleListRead]:
    """[MCP] 列文章（service 视角，无 per-user 过滤）。"""
    articles = svc_list_articles(
        db,
        query=None,
        skip=0,
        limit=limit,
        user_id=None,
        review_status=review_status,
        exclude_distributed=exclude_distributed,
    )
```

其余 body（`if status`、`select(PublishRecord.article_id, func.count())` 那段）不动。

### Step 1.7: 跑 step 1.3 那个 test，确认 pass

```bash
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test \
  pytest server/tests/test_mcp_catalog_articles.py -v
```

Expected: 两个用例都 PASS。

### Step 1.8: 追加第三个 + 第四个 test（保留 failed/cancelled/软删）

- [ ] 在 `test_mcp_catalog_articles.py` 末尾追加：

```python
@pytest.mark.mysql
def test_mcp_list_articles_exclude_distributed_keeps_failed_or_cancelled(monkeypatch):
    """exclude_distributed=True：failed/cancelled 的 record 不算占用，文章应仍出现。

    符合 CLAUDE.md 明文：「failed/cancelled/软删的记录允许重新分发，不永久埋没」。
    """
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        a_failed = _create_approved_article(test_app.client, "失败可重发")
        a_cancelled = _create_approved_article(test_app.client, "取消可重发")
        a_softdel = _create_approved_article(test_app.client, "软删可重发")

        session = test_app.session_factory()
        try:
            pid, acc_id = _setup_platform_and_account(session)
            _add_record(session, a_failed, pid, acc_id, "failed")
            _add_record(session, a_cancelled, pid, acc_id, "cancelled")
            _add_record(session, a_softdel, pid, acc_id, "succeeded", is_deleted=True)
            session.commit()
        finally:
            session.close()

        r = test_app.client.get(
            "/api/mcp/articles?review_status=approved&exclude_distributed=true",
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200, r.text
        ids = {item["id"] for item in r.json()}
        assert a_failed in ids
        assert a_cancelled in ids
        assert a_softdel in ids
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_mcp_list_articles_exclude_distributed_default_false_byte_match(monkeypatch):
    """显式传 exclude_distributed=false 与不传时返回完全一致（防回归 + URL 兼容）。"""
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        a1 = _create_approved_article(test_app.client, "甲")
        a2 = _create_approved_article(test_app.client, "乙")

        session = test_app.session_factory()
        try:
            pid, acc_id = _setup_platform_and_account(session)
            _add_record(session, a1, pid, acc_id, "succeeded")
            session.commit()
        finally:
            session.close()

        headers = {"X-MCP-Token": "secret"}
        r1 = test_app.client.get("/api/mcp/articles?review_status=approved", headers=headers)
        r2 = test_app.client.get(
            "/api/mcp/articles?review_status=approved&exclude_distributed=false",
            headers=headers,
        )
        assert r1.status_code == 200 and r2.status_code == 200
        assert {item["id"] for item in r1.json()} == {item["id"] for item in r2.json()}
    finally:
        test_app.cleanup()
```

### Step 1.9: 跑全套，确认 4 个用例都 pass

```bash
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test \
  pytest server/tests/test_mcp_catalog_articles.py -v
```

Expected: 4 passed.

### Step 1.10: 跑 ruff + mypy 局部，防止 CI fail

```bash
ruff check server/app/modules/articles/service.py server/app/modules/mcp_catalog/router.py server/tests/test_mcp_catalog_articles.py
ruff format --check server/app/modules/articles/service.py server/app/modules/mcp_catalog/router.py server/tests/test_mcp_catalog_articles.py
mypy server/app/modules/articles/service.py server/app/modules/mcp_catalog/router.py
```

Expected: 全绿。如果 ruff format `--check` fail，去掉 `--check` 直接改写再 review diff。

### Step 1.11: commit

```bash
git add server/app/modules/articles/service.py \
  server/app/modules/mcp_catalog/router.py \
  server/tests/test_mcp_catalog_articles.py
git commit -m "$(cat <<'EOF'
feat(articles): list_articles 加 exclude_distributed 参数，与 approved_content_source 同口径

为 /publish 发文 Loop 候选筛选提供「已审未分发」过滤。判定逻辑直接复用
pipeline approved_content_source 节点：PublishRecord.status NOT IN
('failed', 'cancelled') 且未软删 = 已分发/在途。默认 False 防回归。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 新 endpoint `GET /api/tasks/{task_id}/status-mcp`（TDD）

**Files:**
- Test: `server/tests/test_tasks_status_mcp.py`
- Modify: `server/app/modules/tasks/router.py`（末尾追加，与现有 `tasks_mcp_router` 同段）

### Step 2.1: 写测试文件骨架 + 第一个用例（404）

- [ ] 创建 `server/tests/test_tasks_status_mcp.py`：

```python
"""[MCP] GET /api/tasks/{task_id}/status-mcp endpoint 测试。

供 /publish orchestrator poll-to-terminal 用。

覆盖：
- 不存在的 task → 404
- 无 token → 401
- 正常返回 totals 按 status 分组
- is_terminal 与 service.TERMINAL_TASK_STATUSES 完全一致
- failed_records 截断 ≤ 20 条 + 含 error_message
- succeeded_article_ids 与 totals.succeeded 长度一致
"""

from __future__ import annotations

import pytest

from server.app.modules.accounts.models import Account
from server.app.modules.system.models import Platform
from server.app.modules.tasks.models import PublishRecord, PublishTask, PublishTaskAccount


def _setup_platform_and_account(session) -> tuple[int, int]:
    platform = Platform(code="toutiao", name="头条号", base_url="https://mp.toutiao.com")
    account = Account(
        user_id=1,
        platform=platform,
        display_name="测试账号",
        platform_user_id="test-user",
        status="valid",
        state_path="browser_states/toutiao/test/storage_state.json",
    )
    session.add(platform)
    session.add(account)
    session.flush()
    return platform.id, account.id


def _create_task(session, platform_id: int, status: str = "running") -> int:
    task = PublishTask(
        user_id=1,
        name="t",
        task_type="article_round_robin",
        status=status,
        platform_id=platform_id,
        stop_before_publish=False,
    )
    session.add(task)
    session.flush()
    return task.id


def _add_record(
    session, task_id: int, platform_id: int, account_id: int, status: str,
    *, article_id: int = 1, error_message: str | None = None,
) -> int:
    record = PublishRecord(
        task_id=task_id,
        article_id=article_id,
        platform_id=platform_id,
        account_id=account_id,
        status=status,
        error_message=error_message,
    )
    session.add(record)
    session.flush()
    return record.id


@pytest.mark.mysql
def test_status_mcp_404_when_task_not_found(monkeypatch):
    """task 不存在 → 404。"""
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        r = test_app.client.get(
            "/api/tasks/99999/status-mcp",
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 404, r.text
    finally:
        test_app.cleanup()
```

### Step 2.2: 跑 test，看现状

```bash
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test \
  pytest server/tests/test_tasks_status_mcp.py::test_status_mcp_404_when_task_not_found -v
```

Expected: 这个用例**可能在没实现时就 PASS**，因为 FastAPI 对未注册路径默认返 404，恰好与"task 不存在 → 404"的期望误撞。这不是缺陷——它是 endpoint 实现后**防止"误把 404 改成 200 全量列"** 之类回归的基线。真正能驱动 endpoint 实现的失败用例是下面 Step 2.3 的 token 鉴权用例。

### Step 2.3: 追加 token 鉴权用例

- [ ] 在 `test_tasks_status_mcp.py` 末尾追加：

```python
@pytest.mark.mysql
def test_status_mcp_requires_token(monkeypatch):
    """无 token → 401。"""
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()
        test_app.client.cookies.clear()  # 清掉 admin JWT，防止误以为是 user JWT 鉴权

        r = test_app.client.get("/api/tasks/1/status-mcp")
        assert r.status_code == 401, f"无 token 应 401，实际 {r.status_code}"

        r2 = test_app.client.get(
            "/api/tasks/1/status-mcp", headers={"X-MCP-Token": "wrong"}
        )
        assert r2.status_code == 401, f"错 token 应 401，实际 {r2.status_code}"
    finally:
        test_app.cleanup()
```

### Step 2.4: 跑 test，确认 fail

```bash
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test \
  pytest server/tests/test_tasks_status_mcp.py::test_status_mcp_requires_token -v
```

Expected: FAIL（endpoint 还没实现，会 404 而不是 401）。

### Step 2.5: 实现 endpoint

- [ ] 打开 `server/app/modules/tasks/router.py`，定位 `tasks_mcp_router = APIRouter()`（约第 631 行）。
- [ ] 在文件最末尾（`create_task_mcp` 之后）追加：

```python
@tasks_mcp_router.get(
    "/{task_id}/status-mcp",
    dependencies=[Depends(require_mcp_token)],
)
def get_task_status_mcp(task_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """[MCP] Get a publish task's status snapshot for /publish orchestrator polling.

    Returns totals by record status, succeeded_article_ids, and up to 20 failed_records
    so the orchestrator can list failure detail in the Feishu broadcast. is_terminal
    mirrors service.TERMINAL_TASK_STATUSES so the caller does not duplicate the membership.
    """
    task = get_task(db, task_id)
    if task is None or task.is_deleted:
        raise HTTPException(status_code=404, detail=f"Task #{task_id} not found")

    records = list(
        db.execute(
            select(PublishRecord).where(
                PublishRecord.task_id == task_id,
                PublishRecord.is_deleted == False,  # noqa: E712
            )
        )
        .scalars()
        .all()
    )

    totals = {
        "pending": 0,
        "running": 0,
        "succeeded": 0,
        "failed": 0,
        "cancelled": 0,
        "waiting_manual_publish": 0,
        "waiting_user_input": 0,
    }
    succeeded_article_ids: list[int] = []
    failed_records: list[dict[str, Any]] = []
    for r in records:
        totals[r.status] = totals.get(r.status, 0) + 1
        if r.status == "succeeded":
            succeeded_article_ids.append(r.article_id)
        elif r.status == "failed" and len(failed_records) < 20:
            failed_records.append(
                {
                    "record_id": r.id,
                    "account_id": r.account_id,
                    "article_id": r.article_id,
                    "error_message": (r.error_message or "")[:500],
                }
            )

    return {
        "ok": True,
        "data": {
            "task_id": task.id,
            "status": task.status,
            "is_terminal": task.status in TERMINAL_TASK_STATUSES,
            "totals": totals,
            "succeeded_article_ids": succeeded_article_ids,
            "failed_records": failed_records,
            "started_at": task.started_at.isoformat() + "Z" if task.started_at else None,
            "finished_at": task.finished_at.isoformat() + "Z" if task.finished_at else None,
        },
        "error": None,
    }
```

- [ ] 确认文件顶部已 import 这些符号（如缺补上）：
  - `from typing import Any`
  - `from fastapi import HTTPException`
  - `from sqlalchemy import select`
  - `from server.app.modules.tasks.models import PublishRecord`
  - `from server.app.modules.tasks.service import TERMINAL_TASK_STATUSES, get_task`

如果文件已经 import 过其中部分，只补缺的；不要重复 import。

### Step 2.6: 跑两个用例，确认 pass

```bash
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test \
  pytest server/tests/test_tasks_status_mcp.py -v
```

Expected: 2 passed.

### Step 2.7: 追加 totals 分组用例 + is_terminal 用例 + failed_records 截断用例

- [ ] 在 `test_tasks_status_mcp.py` 末尾追加：

```python
@pytest.mark.mysql
def test_status_mcp_totals_grouped_by_status(monkeypatch):
    """totals 按每种 status count 正确，succeeded_article_ids 列出全部 succeeded record。"""
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()
        session = test_app.session_factory()
        try:
            pid, acc_id = _setup_platform_and_account(session)
            task_id = _create_task(session, pid, status="partial_failed")
            _add_record(session, task_id, pid, acc_id, "succeeded", article_id=101)
            _add_record(session, task_id, pid, acc_id, "succeeded", article_id=102)
            _add_record(session, task_id, pid, acc_id, "failed", article_id=103,
                        error_message="boom")
            _add_record(session, task_id, pid, acc_id, "running", article_id=104)
            session.commit()
        finally:
            session.close()

        r = test_app.client.get(
            f"/api/tasks/{task_id}/status-mcp",
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        data = body["data"]
        assert data["status"] == "partial_failed"
        assert data["totals"]["succeeded"] == 2
        assert data["totals"]["failed"] == 1
        assert data["totals"]["running"] == 1
        assert data["totals"]["pending"] == 0
        assert sorted(data["succeeded_article_ids"]) == [101, 102]
    finally:
        test_app.cleanup()


@pytest.mark.mysql
def test_status_mcp_is_terminal_matches_service_constant(monkeypatch):
    """is_terminal 必须与 service.TERMINAL_TASK_STATUSES 字面一致：每个值都验。"""
    from server.app.modules.tasks.service import TERMINAL_TASK_STATUSES
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        # 全部值都应映射 True；任何 'running'/'pending' 应映射 False
        for terminal_status in TERMINAL_TASK_STATUSES:
            session = test_app.session_factory()
            try:
                pid, _ = _setup_platform_and_account(session)
                task_id = _create_task(session, pid, status=terminal_status)
                session.commit()
            finally:
                session.close()
            r = test_app.client.get(
                f"/api/tasks/{task_id}/status-mcp",
                headers={"X-MCP-Token": "secret"},
            )
            assert r.status_code == 200
            assert r.json()["data"]["is_terminal"] is True, (
                f"status={terminal_status!r} 应映射 is_terminal=True"
            )

        # running / pending 应映射 False
        for non_terminal in ("pending", "running"):
            session = test_app.session_factory()
            try:
                pid, _ = _setup_platform_and_account(session)
                task_id = _create_task(session, pid, status=non_terminal)
                session.commit()
            finally:
                session.close()
            r = test_app.client.get(
                f"/api/tasks/{task_id}/status-mcp",
                headers={"X-MCP-Token": "secret"},
            )
            assert r.status_code == 200
            assert r.json()["data"]["is_terminal"] is False


@pytest.mark.mysql
def test_status_mcp_failed_records_capped_at_20_with_error_message(monkeypatch):
    """failed_records 截断到 20 条；每条含 error_message 截断到 500 字符。"""
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()
        session = test_app.session_factory()
        try:
            pid, acc_id = _setup_platform_and_account(session)
            task_id = _create_task(session, pid, status="failed")
            # 25 条 failed
            for i in range(25):
                _add_record(
                    session, task_id, pid, acc_id, "failed",
                    article_id=200 + i, error_message="e" * 600,
                )
            session.commit()
        finally:
            session.close()

        r = test_app.client.get(
            f"/api/tasks/{task_id}/status-mcp",
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["totals"]["failed"] == 25
        assert len(data["failed_records"]) == 20, "failed_records 应截断到 20 条"
        for rec in data["failed_records"]:
            assert len(rec["error_message"]) <= 500, "error_message 应截断到 500 字符"
            assert "record_id" in rec and "account_id" in rec and "article_id" in rec
```

### Step 2.8: 跑全套，确认 5 个 pass

```bash
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test \
  pytest server/tests/test_tasks_status_mcp.py -v
```

Expected: 5 passed.

### Step 2.9: 跑 ruff + mypy

```bash
ruff check server/app/modules/tasks/router.py server/tests/test_tasks_status_mcp.py
ruff format --check server/app/modules/tasks/router.py server/tests/test_tasks_status_mcp.py
mypy server/app/modules/tasks/router.py
```

Expected: 全绿。

### Step 2.10: commit

```bash
git add server/app/modules/tasks/router.py server/tests/test_tasks_status_mcp.py
git commit -m "$(cat <<'EOF'
feat(tasks): GET /api/tasks/{id}/status-mcp endpoint，供 /publish 轮询终态

返回 totals 按 status 分组 + succeeded_article_ids + failed_records 前 20 条
+ is_terminal 与 service.TERMINAL_TASK_STATUSES 共用一处定义。MCP token 鉴权，
与现有 tasks_mcp_router 一致。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 扩 `list_articles` MCP 工具签名 + 加 `get_publish_task_status` 工具

**Files:**
- Modify: `server/mcp/tools/catalog.py`

无新单测——MCP 工具薄包装层由 `test_mcp_tools_async.py` 验「注册成功 + 非死锁」；端点行为在 Task 1 / Task 2 已覆盖。

### Step 3.1: 扩 `list_articles` 工具签名

- [ ] 打开 `server/mcp/tools/catalog.py`，定位第 54 行的 `async def list_articles(...)`，把签名 + body 改成：

```python
@mcp.tool()
async def list_articles(
    status: str | None = None,
    review_status: str | None = None,
    exclude_distributed: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    """List GEO articles with filters.

    Args:
        status: Article workflow status. Common values: "draft", "ready".
        review_status: Editorial review status. Values: "pending", "approved".
        exclude_distributed: If True, also exclude articles with any non-failed,
            non-cancelled, non-deleted PublishRecord. Use this from /publish loop
            to find articles that are approved AND not yet distributed.
        limit: Max number of articles to return (1-100).

    Returns:
        {"ok": True, "data": {"items": [...], "total": N}, "error": None} on success.
        {"ok": False, "data": None, "error": "<message>"} on failure.
    """
    params: dict[str, Any] = {"limit": max(1, min(100, limit))}
    if status:
        params["status"] = status
    if review_status:
        params["review_status"] = review_status
    if exclude_distributed:
        params["exclude_distributed"] = "true"
    return await _aget("/api/mcp/articles", params=params)
```

注意：FastAPI `bool = Query(default=False)` 接受 `"true" / "True" / "1"`。这里显式传 `"true"` 与 spec §3.2 伪码一致。

### Step 3.2: 加 `get_publish_task_status` 工具

- [ ] 在 `server/mcp/tools/catalog.py` 末尾（`list_stock_categories` 之后）追加：

```python
@mcp.tool()
async def get_publish_task_status(task_id: int) -> dict[str, Any]:
    """Get a publish task's current status + per-record breakdown.

    Used by /publish orchestrator to poll until terminal state. Call once every
    ~30s after create_distribute_task returns; stop when data.is_terminal is True.

    Args:
        task_id: PublishTask id, returned by create_distribute_task.

    Returns:
        {"ok": True, "data": {
            "task_id": int,
            "status": str,                  # pending|running|succeeded|partial_failed|failed|cancelled
            "is_terminal": bool,            # True when status is succeeded/partial_failed/failed/cancelled
            "totals": {
                "pending": int,
                "running": int,
                "succeeded": int,
                "failed": int,
                "cancelled": int,
                "waiting_manual_publish": int,
                "waiting_user_input": int,
            },
            "succeeded_article_ids": list[int],
            "failed_records": [             # capped at 20, for Feishu broadcast detail
                {"record_id": int, "account_id": int, "article_id": int, "error_message": str},
                ...
            ],
            "started_at": str | None,        # ISO 8601 UTC
            "finished_at": str | None,
        }, "error": None}
    """
    return await _aget(f"/api/tasks/{task_id}/status-mcp")
```

### Step 3.3: 跑现有 MCP tool 测试，确认未破注册

```bash
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test \
  pytest server/tests/test_mcp_tools_async.py server/tests/test_mcp_catalog.py -v
```

Expected: 全部 PASS。如果 `test_mcp_tools_async.py` 里有按名字列工具的断言，可能因新增 `get_publish_task_status` 让预期集合变小——按报错提示把新工具名加进预期集合。

### Step 3.4: 跑 ruff + mypy

```bash
ruff check server/mcp/tools/catalog.py
ruff format --check server/mcp/tools/catalog.py
mypy server/mcp/tools/catalog.py
```

Expected: 全绿。

### Step 3.5: commit

```bash
git add server/mcp/tools/catalog.py
git commit -m "$(cat <<'EOF'
feat(mcp): list_articles 加 exclude_distributed + 新工具 get_publish_task_status

list_articles 透传 exclude_distributed query 参数到新扩的 /api/mcp/articles。
get_publish_task_status 是 /publish orchestrator poll-to-terminal 用的 ground
truth 查询，调用 GET /api/tasks/{id}/status-mcp，返回 totals + 失败明细前 20 条。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: 写 `templates/commands/publish.md` slash command 模板

**Files:**
- Create: `server/app/modules/loop_skills/templates/commands/publish.md`
- Modify: `server/tests/test_loop_skill_bundle.py:test_build_bundle_lists_all_template_files`（预期集合加 path）

### Step 4.1: 先更新 bundle 文件清单测试（驱动新文件创建）

- [ ] 打开 `server/tests/test_loop_skill_bundle.py`，定位 `test_build_bundle_lists_all_template_files`（第 21 行），把 `paths == { ... }` 集合里加两条新 path（commands/publish.md + skills/geo-publish-orchestrator/SKILL.md，Task 5 写后者）：

```python
    assert paths == {
        "README.md",
        "commands/goal.md",
        "commands/publish.md",
        "skills/geo-goal-orchestrator/SKILL.md",
        "skills/geo-article-writer/SKILL.md",
        "skills/geo-article-verifier/SKILL.md",
        "skills/geo-publish-orchestrator/SKILL.md",
    }
```

### Step 4.2: 跑 test，确认 fail

```bash
pytest server/tests/test_loop_skill_bundle.py::test_build_bundle_lists_all_template_files -v
```

Expected: FAIL（预期里两个新 path 还没有对应文件）。

### Step 4.3: 创建 `commands/publish.md`

- [ ] 创建 `server/app/modules/loop_skills/templates/commands/publish.md`：

````markdown
---
description: Geo 协作平台发文 Loop 入口。自然语言目标 → 自动选号建任务 → Poll 任务终态 → 飞书播报。
---

# /publish — Geo 发文 Loop

你刚被 `/publish $ARGUMENTS` 调用。把这条命令当作 `geo-publish-orchestrator`
skill 的入口包装：

1. **立刻** invoke the `geo-publish-orchestrator` skill（用 Skill tool）来装载完整 playbook。
2. 装载后，按 skill 里的 Required Checklist 一项一项执行，把 `$ARGUMENTS` 当作用户的自由文本目标传给「Goal Parsing 规则」段。
3. **不要**在装载 skill 之前先自己解析目标或调 MCP；skill 内部第一步就是 sanity check，让它来跑。

## 同事第一次用 /publish 之前要看的

如果是你（同事）第一次用 `/publish`，先确认前端「MCP 接入」tab 已经把最新
bundle 装到了本地（`~/.claude/commands/publish.md` 应该存在）；不然 Claude
Code 找不到这条命令。重装见 `.claude/README.md`（也由 bundle 分发）。

## 这条命令做什么 / 不做什么

**做**：
- 自然语言目标解析（"今天发 5 篇头条"）
- 从已审核未分发库选 N 篇文章
- 启发式按账号近 7 天表现选号
- 创建 article_round_robin 分发任务
- Poll 任务终态后飞书播报

**不做**：
- 不写文章 / 不评分（那是 /goal 的事）
- 不改 article.review_status（人审审过的才进候选）
- 不重试已失败的 record（lever-a 后失败=expired 账号，重登录路径独立）
- 不取消运行中的 task（Ctrl-C 不杀 task，task 后台自己跑完）
````

### Step 4.4: 不 commit；先做 Task 5（一起 commit 才让 bundle sha 稳定）

Task 6 才 bump version + 算 sha。这一步留着 commit 等 Task 5、6 一起做（避免中间状态 bundle sha 不稳）。

---

## Task 5: 写 `templates/skills/geo-publish-orchestrator/SKILL.md`

**Files:**
- Create: `server/app/modules/loop_skills/templates/skills/geo-publish-orchestrator/SKILL.md`

### Step 5.1: 创建目录 + 写 SKILL.md

- [ ] 确认目录存在（如不存在创建）：`server/app/modules/loop_skills/templates/skills/geo-publish-orchestrator/`
- [ ] 创建 `SKILL.md`：

````markdown
---
name: geo-publish-orchestrator
description: Use when /publish command is invoked in geo-collab repo. Drives the
  heuristic-selected publish task with a single article_round_robin big task and
  poll-to-terminal stop condition. Owns natural-language goal parsing, candidate
  selection, account ranking by 7d metrics, and Feishu reporting.
---

# Role

你是 `/publish` 命令的 orchestrator。在**主对话**里执行；不起 subagent。
你只做：sanity check → 解析目标 → 候选筛选 + 启发式选号 → 建 task → poll 到终态 → 飞书播报。

# Required Checklist (per /publish invocation)

1. **Sanity check** — 调 `list_question_pools()`；失败立即退出 + 提示
   "请按 docs/mcp-setup-notes.md 配 ~/.claude.json 的 mcpServers.geo"
2. **解析目标** — 从用户自由文本抽取 `{N, platform_code, dry_run}`
3. **拉候选 + 账号 + metrics** — `list_articles(exclude_distributed=True)` +
   `list_accounts` + `get_account_performance(7)`
4. **启发式选号选文** — 见下「启发式选号」段
5. **建 task** — `create_distribute_task(stop_before_publish=dry_run)`
6. **Poll 到终态** — 每 30s 调 `get_publish_task_status(task_id)`；超时 30 min
7. **飞书播报** — `notify_feishu(title, message, level)`

# Goal Parsing 规则

| 字段 | 抽取规则 | 缺省 |
|---|---|---|
| `N` | 文中数字 + 量词（"5 篇" / "8 个" / "10 件"） | **必须明确，不可解析时反问退出，不默认 5**（发文有真实成本，不猜） |
| `platform_code` | 见到"头条" → `toutiao`；"微信公众号" → `wechat_mp` | `"toutiao"` |
| `dry_run` | 见到"演练" / "dry run" / "只选不发" / "预览不发" | `False` |

# 候选选文规则

- `list_articles(review_status="approved", status="ready", exclude_distributed=True, limit=N+10)`
- 后端默认按 `updated_at` desc 排序
- 候选不足 N 篇 → 飞书 warning「请先跑 /goal 补库」+ 退出
- MVP **不**按"题材匹配账号"二次过滤（YAGNI）

# 启发式选号

- 过滤：`status=valid` + `distribution_enabled=True`
- 排序：按 `get_account_performance(account.id, 7).data.avg_views` 倒序；缺失值排末位
- 取前 `min(N, len(accounts))` 个
- 账号不够 → 少派号、不等账号；可用账号为 0 → 飞书 error + 退出

# 主循环（伪码）

```pseudo
notify_feishu(title="发文 Loop 开始",
              message=f"目标 {target.N} 篇 / 平台 {target.platform_code}", level="info")

# 1. 候选准备
articles = list_articles(
    review_status="approved", status="ready",
    exclude_distributed=True, limit=target.N + 10,
).data.items
if len(articles) < target.N:
    notify_feishu("发文 Loop 中止",
        f"已审未分发候选不足：{len(articles)}/{target.N}，请先跑 /goal 补库", "warning")
    return ABORT

accounts = list_accounts(platform_code=target.platform_code, distribution_enabled=True).data
accounts = [a for a in accounts if a.status == "valid"]
if not accounts:
    notify_feishu("发文 Loop 中止",
        f"无可用 {target.platform_code} 账号", "error")
    return ABORT

# 2. 启发式选号（按 7 天 metrics 排序）
metrics = {a.id: (get_account_performance(a.id, 7).data or {}) for a in accounts}
accounts.sort(key=lambda a: metrics[a.id].get("avg_views") or 0, reverse=True)
selected_accounts = accounts[:min(target.N, len(accounts))]
selected_articles = articles[:target.N]

# 3. 建任务
r = create_distribute_task(
    name=f"发文 Loop · {today} · {target.N} 篇",
    article_ids=[a.id for a in selected_articles],
    account_ids=[a.id for a in selected_accounts],
    platform_code=target.platform_code,
    stop_before_publish=target.dry_run,
)
if not r.ok:
    notify_feishu("发文 Loop 中止", f"建任务失败：{r.error}", "error")
    return ABORT
task_id = r.data.task_id
notify_feishu("发文任务已派",
    f"task #{task_id} · {target.N} 篇 → {len(selected_accounts)} 账号", "info")
echo(f"[任务已建] task #{task_id}，{target.N} 篇 → {len(selected_accounts)} 账号")

# 4. Poll（每 30s 用 Bash sleep 30 阻塞）
poll_started_at = now()
consecutive_mcp_fail = 0
while True:
    s = get_publish_task_status(task_id)
    if not s.ok:
        consecutive_mcp_fail += 1
        if consecutive_mcp_fail >= 3:
            notify_feishu("发文 Loop 中止", "MCP 连续失败 3 次", "error")
            return ABORT
        bash("sleep 30"); continue
    consecutive_mcp_fail = 0

    echo(f"[进度] task #{task_id} {s.data.status}"
         f" 成功 {s.data.totals.succeeded}/{target.N}"
         f" 在跑 {s.data.totals.running} 失败 {s.data.totals.failed}")

    if s.data.is_terminal:
        break
    if now() - poll_started_at > 30 * 60:
        notify_feishu("发文 Loop 部分完成",
            f"task #{task_id} 30 min 未达终态，当前 succeeded {s.data.totals.succeeded}/{target.N}，"
            f"请去分发引擎 tab 查后续", "warning")
        return PARTIAL
    bash("sleep 30")

# 5. 终态播报
accounts_refresh = list_accounts(
    platform_code=target.platform_code, distribution_enabled=True).data
expired_now = {a.id: a for a in accounts_refresh if a.status == "expired"}
expired_in_run = [a for a in selected_accounts if a.id in expired_now]

succeeded = s.data.totals.succeeded
failed = s.data.totals.failed
if s.data.status == "succeeded" and succeeded == target.N:
    level = "done"
elif succeeded == 0:
    level = "error"
else:
    level = "warning"

msg = (
    f"task #{task_id} 终态：{s.data.status}\n"
    f"成功 {succeeded}/{target.N}、失败 {failed}\n"
)
if expired_in_run:
    msg += f"失效账号需重登录：{', '.join(a.username for a in expired_in_run)}\n"
if failed > 0:
    msg += "失败明细：\n" + "\n".join(
        f"  · article #{r.article_id} → account #{r.account_id}: {r.error_message[:80]}"
        for r in s.data.failed_records[:5]
    )
title_map = {
    "done": "发文 Loop 完成",
    "warning": "发文 Loop 完成（部分失败）",
    "error": "发文 Loop 失败",
}
notify_feishu(title_map[level], msg, level=level)
```

# Helper 定义（消除歧义）

| Helper | 定义 |
|---|---|
| `now()` | 主对话本地时钟（不依赖 MCP），用于 poll 超时计时 |
| `bash("sleep 30")` | 用 Bash tool 跑 `sleep 30`（PowerShell host 上等价 `Start-Sleep -Seconds 30`）；主对话本身没有原生 sleep，靠 Bash 阻塞 30s |
| `is_mcp_error(error)` | `mcp__geo__*` 返回 `{ok:false, error}` 或抛 401/502/5xx/超时 → True |
| `today` | 用本地日期 ISO 字符串如 `"2026-06-26"`，用于 task 命名 |

# 进度日志（必须 echo 这些短行）

```
[启动检查] 平台：toutiao　目标：5 篇　✓
[候选] 已审未分发文章 12 篇，账号 6 个（valid 5、expired 1）
[选号] 按 7 天 avg_views 取前 5：account #3, #1, #7, #2, #5
[任务已建] task #142，5 篇 → 5 账号
[进度] task #142 running 成功 1/5 在跑 2 失败 0
[进度] task #142 running 成功 3/5 在跑 1 失败 0
[完成] task #142 succeeded 5/5，耗时 8 分钟，飞书已播报
```

# 主对话叙述规范（强制）

你向用户叙述本次 /publish 运行时，**只能用中文 + 上面进度日志的固定格式**。
绝对不要在叙述里出现以下英文 / 内部术语（左侧错例，右侧用法）：

| ❌ 不要说 | ✅ 改成 |
|---|---|
| orchestrator | 编排员 / 我 |
| record / publish_record | 发布记录 |
| dry_run | 演练 / 只选不发 |
| platform_code | 平台 |
| article_round_robin | 多账号轮转分发 |
| stop_before_publish | 停在预览（仅演练时） |
| poll / polling | 轮询 / 等任务跑完 |
| expired | 失效（账号需重登录） |
| terminal | 终态 |
| heuristic | 启发式选号 / 按表现排序 |

**反例**（千万别这样说）：

> 启动 orchestrator。N=5 platform=toutiao。先 poll task status。

**正例**：

> 开始执行 /publish：目标 5 篇头条。先看一下候选和账号。

# Stop / Budget Rules

- `s.data.is_terminal == True` → SUCCESS / 部分成功 / 失败（按 succeeded/N 决定 level）
- Poll 累计 > 30 min → PARTIAL（飞书 warning，task 后台继续）
- 连续 MCP 错误 >= 3 → ABORT（飞书 error）
- 候选不足 → ABORT（飞书 warning）
- 无可用账号 → ABORT（飞书 error）
- 用户 Ctrl-C → 主对话 echo `[已中断] task #X 仍在后台，请去分发引擎 tab 看进度`（不发飞书）

# 三个不变式（硬约束）

1. **不建任务前的失败不杀 task**：任何检查/选号阶段失败，loop 退出就好，**不要**留半建的 task
2. **建任务后的失败不杀 task**：task 一旦建好（返回 task_id），无论 poll 是否完成、orchestrator 是否退出、用户是否 Ctrl-C，task 自己后台跑完。loop 退出 ≠ task 退出
3. **Ground truth 是 PublishRecord.succeeded**：自然语言里的"5 篇发完了"必须来自 `get_publish_task_status.totals.succeeded`，**绝不**用 task.status 单独推断
````

### Step 5.2: 跑 bundle 文件清单测试，确认 pass

```bash
pytest server/tests/test_loop_skill_bundle.py::test_build_bundle_lists_all_template_files -v
```

Expected: PASS。如果 fail，检查目录路径 + 文件名拼写。

### Step 5.3: 跑 sha 校验测试，预期 **fail**（因为还没 bump version + sha）

```bash
pytest server/tests/test_loop_skill_bundle.py::test_bundle_sha_is_known -v
```

Expected: FAIL with `Bundle sha256 = '<新 sha>' not in KNOWN_BUNDLE_SHAS`. 把报错里的新 sha 字符串**记下来**——Task 6 第一步要填。

### Step 5.4: 不 commit；进入 Task 6 bump version 之后一起 commit

---

## Task 6: bump `LOOP_SKILL_BUNDLE_VERSION` + 加 2 个新 sha

**Files:**
- Modify: `server/app/modules/loop_skills/version.py`

### Step 6.1: 在 Windows host 直接读 Step 5.3 报错里的 sha

把 Step 5.3 报错里 `Bundle sha256 = '<sha>'` 的字符串记成 `<CRLF_SHA>`。这是 Windows host 上 Git checkout 默认 CRLF 行尾的 sha。

### Step 6.2: 拿 LF 行尾的 sha

CI 在 Linux 上跑，模板 checkout 是 LF。本地用 dev 容器跑一下 build_bundle 拿 LF sha：

```bash
docker compose exec app python -c "from server.app.modules.loop_skills.service import build_bundle; print(build_bundle().bundle_sha256)"
```

记成 `<LF_SHA>`。

（备选方案：先把 `<CRLF_SHA>` 加进 KNOWN_BUNDLE_SHAS、commit、push，让 CI 报错告诉你 LF sha 是什么，再补 commit。任选其一。）

### Step 6.3: 改 `version.py`

- [ ] 打开 `server/app/modules/loop_skills/version.py`：

```python
LOOP_SKILL_BUNDLE_VERSION = "2026-06-26-v1"
```

- [ ] 在 `KNOWN_BUNDLE_SHAS` 集合末尾（保留所有现有条目）追加：

```python
        # v6 (2026-06-26): +publish.md + geo-publish-orchestrator/SKILL.md
        # for /publish 发文 Loop（spec: 2026-06-26-publish-loop-engineering-design.md）
        "<CRLF_SHA>",  # CRLF (Windows host)
        "<LF_SHA>",    # LF (CI canonical)
```

替换 `<CRLF_SHA>` / `<LF_SHA>` 为 Step 6.1 / 6.2 实际拿到的值。版本号 `2026-06-26-v1` 与现有 `2026-06-25-v4` 顺延即可；如果 main 在此期间已 bump 到更大版本，本 PR 把数字再 +1。

### Step 6.4: 跑 sha 校验测试，确认 pass

```bash
pytest server/tests/test_loop_skill_bundle.py -v
```

Expected: 全部 PASS（包括 `test_bundle_sha_is_known` + `test_build_bundle_lists_all_template_files`）。

### Step 6.5: 跑 ruff + mypy

```bash
ruff check server/app/modules/loop_skills/version.py server/tests/test_loop_skill_bundle.py
ruff format --check server/app/modules/loop_skills/version.py server/tests/test_loop_skill_bundle.py
mypy server/app/modules/loop_skills/version.py
```

Expected: 全绿。

### Step 6.6: commit（一次合并 Task 4 + 5 + 6 的全部改动）

```bash
git add server/app/modules/loop_skills/templates/commands/publish.md \
  server/app/modules/loop_skills/templates/skills/geo-publish-orchestrator/SKILL.md \
  server/app/modules/loop_skills/version.py \
  server/tests/test_loop_skill_bundle.py
git commit -m "$(cat <<'EOF'
feat(loop_skills): /publish slash command + geo-publish-orchestrator skill 模板正本

bundle v6（2026-06-26-v1）：新增 publish.md 命令包装 + 完整 orchestrator
skill（含主循环伪码 / 启发式选号 / 中文叙述规范 / 三个不变式）。同事经前端
「MCP 接入」tab 重装即可拿到 /publish 入口。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: 端到端验证（CI 全绿后）

### Step 7.1: 推送 + 看 CI

```bash
git push origin <branch-name>
gh pr create --title "feat(loop): /publish 发文 Loop Engineering（spec 2026-06-26）" \
  --body "$(cat <<'EOF'
## Summary

- 后端：`list_articles` 加 `exclude_distributed`，新 endpoint `GET /api/tasks/{id}/status-mcp`
- MCP 工具：扩 `list_articles` 签名 + 新 `get_publish_task_status`
- 模板：`publish.md` slash command + `geo-publish-orchestrator/SKILL.md`，bundle v6
- 测试：4 个 `list_articles.exclude_distributed` 用例 + 5 个 `status-mcp` 用例 + 现有 `test_loop_skill_bundle.py` sha 校验

设计：`docs/superpowers/specs/2026-06-26-publish-loop-engineering-design.md`
Plan：`docs/superpowers/plans/2026-06-26-publish-loop-engineering.md`

## Test plan

- [ ] CI ruff / format / mypy / pytest 全绿
- [ ] 本地手工冒烟 12 步（spec §9.3）
- [ ] 至少 1 个非作者同事经「MCP 接入」tab 重装并独立跑通 `/publish 1 篇`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

如 CI 因 LF sha 不匹配 fail，从 Step 6.2 备选路径补 LF sha 后再推。

### Step 7.2: 手工冒烟（按 spec §9.3 12 步走一遍）

前置：本地有 ≥ 1 个 `status=valid` 的 toutiao 账号，已审未分发库里有 ≥ 1 篇文章。

- [ ] 前端「MCP 接入」tab 看到新 bundle 版本提示 → 点重装
- [ ] Claude Code 重启 → 输入 `/mcp`，确认 `get_publish_task_status` 工具已注册
- [ ] 跑 `/publish 帮我发 1 篇头条作为冒烟`
- [ ] 主对话 echo `[启动检查] ... ✓`
- [ ] 主对话 echo `[候选] 已审未分发 X 篇，账号 Y 个`
- [ ] 主对话 echo `[任务已建] task #X，1 篇 → 1 账号`
- [ ] 每 30s 主对话 echo `[进度] task #X running 成功 0/1 在跑 1`
- [ ] 终态 echo `[完成] task #X succeeded 1/1，耗时 X 分钟`
- [ ] 飞书群有「发文 Loop 完成」播报
- [ ] GEO 前端「分发引擎」tab 该 task 显示 succeeded
- [ ] 头条该账号实际发出文章
- [ ] **故障演练**：把 1 个账号置 `status=expired` 后再跑 `/publish 1 篇` —— 选号阶段应过滤掉它；如全部 expired，飞书 error
- [ ] **故障演练**：跑 `/publish 一下`（N 不可解析）—— 主对话反问「请明确写几篇」，**不**默认 5

### Step 7.3: 找 1 个非作者同事独立跑通

让另一位同事按「MCP 接入」tab 的引导走一遍：
1. 看到新版本提示 → 点重装
2. 重启 Claude Code
3. 跑 `/publish 帮我发 1 篇头条`
4. 等飞书播报

记录他/她遇到的任何文案 / 路径含糊点，补到 SKILL.md 的「常见排障」段（Task 5 已有，按需追加）。

如有补充，按 Task 5/6 流程：改模板 → bump version → 重新算 sha → 加进 KNOWN_BUNDLE_SHAS → 再走 CI。

---

## 上线门禁

- spec §9.1 全部 9 个新用例 + 现有 CI 全绿（Task 1 + Task 2 + Task 6 共 10 个 pytest 用例集）
- §9.3 12 步手工冒烟全过（含 2 个故障演练，Step 7.2）
- 至少 1 个非作者同事经「MCP 接入」tab 重装并独立跑通 `/publish 1 篇`（Step 7.3）

任一不满足 → 不合并。
