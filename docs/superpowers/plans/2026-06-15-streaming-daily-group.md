# pipeline 生文「边生成边进每日分组」实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 pipeline 的 `ai_generate` 节点加 `daily_group` 开关，开启后生成前先建好「每日生成·日期」分组、每篇生成成功立即进组并标待审，运行中可实时观察、中途失败不丢已生成的文章。

**Architecture:** 复用现有 daily_group 的日期分组机制（`每日生成·YYYY-MM-DD` + 同日复用 + 去重），把成组从「整批最后一次」提前为「生成前建组 + 每篇流式追加」。新增两个 service helper（建组 / 单篇追加），ai_generate 在 flat 与 units 两条路径里调用，to_review 加守卫避免双重成组。并发安全靠：组提前建一次、每 worker 只碰各自的文章行 + 各自的 item 行、append 绝不更新组行、sort_order 用进程内计数器（不走 DB `FOR UPDATE`）。

**Tech Stack:** Python / FastAPI / SQLAlchemy / MySQL（InnoDB）/ pytest（`@pytest.mark.mysql`，需 `GEO_TEST_DATABASE_URL`）。零前端改动（toggle 由前端通用渲染器自动渲染）。

**Spec:** `docs/superpowers/specs/2026-06-15-streaming-daily-group-design.md`

---

## 运行测试的命令（贯穿全程）

所有新测试都是 `@pytest.mark.mysql`，需要 MySQL 测试库（库名必须含 `test`）。激活 conda 环境 `geo_xzpt` 后运行（注意：工具 shell 里 `conda activate` 可能不生效，必要时用该环境 python 的全路径）：

```bash
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test \
  pytest server/tests/test_streaming_daily_group.py -q
```

末尾 CI 门禁（全绿才算完）：

```bash
ruff check server/
ruff format --check server/
mypy server/app
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test pytest server/tests/ -q
```

> 本计划**零前端改动**，前端 typecheck/build 不受影响，无需改动；如要保险可在最后跑一次 `pnpm --filter @geo/web typecheck`。

---

## File Structure

- **Modify** `server/app/modules/articles/service.py` — 新增 `resolve_or_create_daily_group`、`append_article_to_group_pending` 两个 helper。
- **Modify** `server/app/modules/pipelines/nodes/ai_generate_node.py` — 新增 `_make_group_streamer`，在 `_run_units` 与扁平路径接入流式进组，节点输出加 `group_id`。
- **Modify** `server/app/modules/pipelines/router.py:86-94` — ai_generate 的 `config_schema` 加 `daily_group` toggle。
- **Modify** `server/app/modules/pipelines/nodes/to_review.py:17` — 加「上游已成组就透传」守卫。
- **Create** `server/tests/test_streaming_daily_group.py` — 全部新测试。
- **Modify** `CLAUDE.md` — pipelines 模块段补一句 ai_generate `daily_group` 流式说明。

---

## Task 1: service 层两个 helper（建组 + 单篇追加）

**Files:**
- Modify: `server/app/modules/articles/service.py`（在 `mark_pending_and_append_daily` 之后追加，约 line 668 之后）
- Test: `server/tests/test_streaming_daily_group.py`（新建）

参考：现有 `mark_pending_and_append_daily`（[service.py:572-667](../../../server/app/modules/articles/service.py)）的复用/复活/IntegrityError 重试写法；本任务把「建组」与「逐篇追加」拆开，且追加时**绝不动组行**（区别于 line 654 的 `group.updated_at = utcnow()`）。模块已 import `func`、`select`、`utcnow`、`Article`、`ArticleGroup`、`ArticleGroupItem`，并已有模块级 `_logger`。

- [ ] **Step 1: 写失败测试（建测试文件 + 共享 helper + service 级用例）**

Create `server/tests/test_streaming_daily_group.py`：

```python
import uuid

import pytest

from server.tests.utils import build_test_app


# ---- 共享 helper（本文件后续任务的测试都复用） ----
def _make_article(client, title="文章"):
    r = client.post(
        "/api/articles",
        json={
            "title": title,
            "content_json": {"type": "doc", "content": []},
            "content_html": "<p>x</p>",
            "plain_text": "x",
            "word_count": 1,
            "status": "ready",
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _uid(app):
    from server.app.modules.system.models import User

    with app.session_factory() as db:
        return db.query(User).first().id


def _fake_generate(*, session_factory, user_id, template_content, question_text, model=None):
    """与 generate_article_from_prompt 同签名：建一篇文章、返回 id。"""
    from server.app.modules.articles.schemas import ArticleCreate
    from server.app.modules.articles.service import create_article

    db = session_factory()
    try:
        art = create_article(
            db,
            user_id,
            ArticleCreate(
                title=f"A-{uuid.uuid4().hex[:6]}",
                content_json={"type": "doc", "content": []},
                content_html="<p>x</p>",
                plain_text="x",
                word_count=1,
                client_request_id=str(uuid.uuid4()),
            ),
        )
        db.commit()
        return art.id
    finally:
        db.close()


def _make_tpl(app, uid, enabled=True):
    from server.app.modules.prompt_templates.models import PromptTemplate

    with app.session_factory() as db:
        t = PromptTemplate(
            name="模板", content="写: {{question}}", scope="generation",
            user_id=uid, is_enabled=enabled,
        )
        db.add(t)
        db.commit()
        return t.id


def _ctx(app, uid, config, inputs, upstream=None):
    from server.app.modules.pipelines.nodes.base import NodeRunContext

    return NodeRunContext(
        session_factory=app.session_factory,
        user_id=uid, config=config, inputs=inputs, upstream=upstream or {},
    )


# ---- Task 1: service helper ----
@pytest.mark.mysql
def test_resolve_new_then_reuse_with_next_sort(monkeypatch):
    from server.app.modules.articles.models import ArticleGroup, ArticleGroupItem
    from server.app.modules.articles.service import resolve_or_create_daily_group

    app = build_test_app(monkeypatch)
    try:
        uid = _uid(app)
        NAME = "每日生成 · 2026-06-15"
        # 首建 → (gid, 0)
        res1 = resolve_or_create_daily_group(app.session_factory, user_id=uid, group_name=NAME)
        assert res1 is not None
        gid, start = res1
        assert start == 0
        # 塞两个 item（sort_order 0,1），再 resolve → 复用同组、next_start=2
        with app.session_factory() as db:
            db.add(ArticleGroupItem(group_id=gid, article_id=_make_article(app.client), sort_order=0))
            db.add(ArticleGroupItem(group_id=gid, article_id=_make_article(app.client), sort_order=1))
            db.commit()
        res2 = resolve_or_create_daily_group(app.session_factory, user_id=uid, group_name=NAME)
        assert res2 == (gid, 2)
        with app.session_factory() as db:
            cnt = db.query(ArticleGroup).filter(
                ArticleGroup.user_id == uid, ArticleGroup.is_deleted == False  # noqa: E712
            ).count()
            assert cnt == 1  # 复用、没新建
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_resolve_revives_soft_deleted(monkeypatch):
    from server.app.modules.articles.models import ArticleGroup
    from server.app.modules.articles.service import resolve_or_create_daily_group

    app = build_test_app(monkeypatch)
    try:
        uid = _uid(app)
        NAME = "每日生成 · 2026-06-15"
        with app.session_factory() as db:
            g = ArticleGroup(user_id=uid, name=NAME, is_deleted=True)
            db.add(g)
            db.commit()
            old_gid = g.id
        res = resolve_or_create_daily_group(app.session_factory, user_id=uid, group_name=NAME)
        assert res is not None
        gid, start = res
        assert gid == old_gid and start == 0  # 复活同一行、空成员
        with app.session_factory() as db:
            assert db.get(ArticleGroup, gid).is_deleted is False
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_append_marks_pending_inserts_item_and_leaves_group_row_untouched(monkeypatch):
    from server.app.modules.articles.models import Article, ArticleGroup, ArticleGroupItem
    from server.app.modules.articles.service import (
        append_article_to_group_pending,
        resolve_or_create_daily_group,
    )

    app = build_test_app(monkeypatch)
    try:
        uid = _uid(app)
        gid, _ = resolve_or_create_daily_group(
            app.session_factory, user_id=uid, group_name="每日生成 · 2026-06-15"
        )
        with app.session_factory() as db:
            g = db.get(ArticleGroup, gid)
            ver0, upd0 = g.version, g.updated_at
        aid = _make_article(app.client)

        ok = append_article_to_group_pending(
            app.session_factory, group_id=gid, article_id=aid, sort_order=5
        )
        assert ok is True
        with app.session_factory() as db:
            assert db.get(Article, aid).review_status == "pending"  # 标待审
            items = db.query(ArticleGroupItem).filter(ArticleGroupItem.group_id == gid).all()
            assert len(items) == 1 and items[0].article_id == aid and items[0].sort_order == 5
            g = db.get(ArticleGroup, gid)
            assert g.version == ver0 and g.updated_at == upd0  # 组行未被动（防死锁的关键）
        # 重复追加同一篇 → 去重、不报错
        ok2 = append_article_to_group_pending(
            app.session_factory, group_id=gid, article_id=aid, sort_order=9
        )
        assert ok2 is True
        with app.session_factory() as db:
            cnt = db.query(ArticleGroupItem).filter(ArticleGroupItem.group_id == gid).count()
            assert cnt == 1
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_resolve_reuses_after_concurrent_create(monkeypatch):
    """resolve 内首次 flush 前另一会话抢先建好同名组 → 撞唯一约束后回查复用（覆盖 IntegrityError/
    OperationalError 同一 except 分支）。"""
    from server.app.modules.articles import service as svc
    from server.app.modules.articles.models import ArticleGroup

    app = build_test_app(monkeypatch)
    try:
        uid = _uid(app)
        NAME = "每日生成 · 2026-06-15"
        real_factory = app.session_factory
        state = {"injected": False, "concurrent_gid": None}

        class _HookSession:
            def __init__(self, inner):
                object.__setattr__(self, "_inner", inner)

            def __getattr__(self, name):
                return getattr(object.__getattribute__(self, "_inner"), name)

            def flush(self, *args, **kwargs):
                inner = object.__getattribute__(self, "_inner")
                if not state["injected"]:
                    state["injected"] = True
                    with real_factory() as other:  # 模拟并发：另一会话抢先建组
                        g = ArticleGroup(user_id=uid, name=NAME)
                        other.add(g)
                        other.commit()
                        state["concurrent_gid"] = g.id
                return inner.flush(*args, **kwargs)  # 本会话重复插入在此自然撞 IntegrityError

        def hook_factory():
            return _HookSession(real_factory())

        res = svc.resolve_or_create_daily_group(hook_factory, user_id=uid, group_name=NAME)
        assert res is not None
        gid, start = res
        assert gid == state["concurrent_gid"] and start == 0  # 回查复用了并发建的组
    finally:
        app.cleanup()
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `GEO_TEST_DATABASE_URL=... pytest server/tests/test_streaming_daily_group.py -q`
Expected: FAIL — `ImportError: cannot import name 'resolve_or_create_daily_group'`（helper 还没实现）。

- [ ] **Step 3: 实现两个 helper**

在 `server/app/modules/articles/service.py` 的 `mark_pending_and_append_daily` 函数**之后**追加：

```python
def resolve_or_create_daily_group(
    session_factory,
    *,
    user_id: int,
    group_name: str,
) -> tuple[int, int] | None:
    """查找-或-新建 (user_id, group_name) 分组，返回 (group_id, next_sort_order_start)。

    - 未软删同名组 → 复用；软删同名 → 复活清空成员；都没有 → 新建。
    - next_sort_order_start = 现有 max(sort_order)+1（空组/新建/复活 → 0）。
    - 并发首建撞 (user_id, name) 唯一约束 → rollback 回查复用（catch IntegrityError 与
      OperationalError：InnoDB 并发唯一 INSERT 偶发死锁 1213）；回查仍无 → 抛到外层返回 None。
    - 只解析/建组，不标 pending、不插 item。独立 session、本函数内 commit+close。失败记日志返回 None。
    详见 docs/superpowers/specs/2026-06-15-streaming-daily-group-design.md。"""
    try:
        from sqlalchemy.exc import IntegrityError, OperationalError

        db = session_factory()
        try:

            def _resolve() -> ArticleGroup:
                existing = (
                    db.query(ArticleGroup)
                    .filter(ArticleGroup.user_id == user_id, ArticleGroup.name == group_name)
                    .first()
                )
                if existing is not None:
                    if existing.is_deleted:  # 软删同名 → 复活并清空旧成员
                        existing.is_deleted = False
                        existing.deleted_at = None
                        existing.version += 1
                        existing.updated_at = utcnow()
                        existing.items.clear()
                        db.flush()
                    return existing
                grp = ArticleGroup(user_id=user_id, name=group_name)
                db.add(grp)
                db.flush()  # 撞唯一约束在此抛 IntegrityError（并发偶发 OperationalError/死锁）
                return grp

            try:
                group = _resolve()
            except (IntegrityError, OperationalError):
                db.rollback()
                group = (
                    db.query(ArticleGroup)
                    .filter(
                        ArticleGroup.user_id == user_id,
                        ArticleGroup.name == group_name,
                        ArticleGroup.is_deleted.is_(False),
                    )
                    .first()
                )
                if group is None:
                    raise

            max_order = (
                db.query(func.max(ArticleGroupItem.sort_order))
                .filter(ArticleGroupItem.group_id == group.id)
                .scalar()
            )
            next_start = (max_order + 1) if max_order is not None else 0
            gid = group.id
            db.commit()
            return gid, next_start
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — 尽力而为
        _logger.exception(
            "resolve_or_create_daily_group failed (user=%s, name=%s)", user_id, group_name
        )
        return None


def append_article_to_group_pending(
    session_factory,
    *,
    group_id: int,
    article_id: int,
    sort_order: int,
) -> bool:
    """把单篇标 review_status='pending' 并追加进已存在的 group_id（只插 item、不动组行）。

    - 绝不 UPDATE 组行（不 bump version/updated_at）——避免并发 worker 抢父行排他锁（见 spec 第 7 节）。
    - 撞 (group_id, article_id) 唯一约束（理论上不会，文章是本次新建的）→ 当作已在组、忽略。
    - 独立 session、commit+close。失败记日志返回 False。"""
    try:
        from sqlalchemy.exc import IntegrityError

        db = session_factory()
        try:
            art = db.get(Article, article_id)
            if art is not None:
                art.review_status = "pending"
            db.add(ArticleGroupItem(group_id=group_id, article_id=article_id, sort_order=sort_order))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()  # item 已存在 → 仅补标 pending
                art2 = db.get(Article, article_id)
                if art2 is not None and art2.review_status != "pending":
                    art2.review_status = "pending"
                    db.commit()
            return True
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        _logger.exception(
            "append_article_to_group_pending failed (group=%s, article=%s)", group_id, article_id
        )
        return False
```

- [ ] **Step 4: 跑测试，确认通过**

Run: `GEO_TEST_DATABASE_URL=... pytest server/tests/test_streaming_daily_group.py -q`
Expected: PASS（4 个 Task 1 用例全过）。

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/articles/service.py server/tests/test_streaming_daily_group.py
git commit -m "feat(articles): 新增日期分组建组/单篇追加 helper（流式进组用）"
```

---

## Task 2: ai_generate 流式进组（flat + units 两路径）

**Files:**
- Modify: `server/app/modules/pipelines/nodes/ai_generate_node.py`
- Test: `server/tests/test_streaming_daily_group.py`（追加用例，复用 Task 1 的 helper）

参考当前结构：`_run_units(ctx, cfg, units, model, max_count)`（[ai_generate_node.py:58-107](../../../server/app/modules/pipelines/nodes/ai_generate_node.py)）；扁平路径在 `run_ai_generate`（[:110-180](../../../server/app/modules/pipelines/nodes/ai_generate_node.py)）。两路径各有内嵌 `_one()` 和各自的 `ThreadPoolExecutor`。

- [ ] **Step 1: 写失败测试（追加到测试文件末尾）**

```python
# ---- Task 2: ai_generate 流式进组 ----
def _patch_generate(monkeypatch):
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_generate_node.generate_article_from_prompt",
        _fake_generate,
    )


@pytest.mark.mysql
def test_flat_streams_each_article_into_today_group(monkeypatch):
    from server.app.modules.articles.models import Article, ArticleGroup, ArticleGroupItem

    _patch_generate(monkeypatch)
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_generate_node import run_ai_generate

        uid = _uid(app)
        t = _make_tpl(app, uid)
        ctx = _ctx(
            app, uid,
            {"prompt_template_id": t, "count": 3, "model": None, "daily_group": True},
            {"question_text": "q"},
        )
        res = run_ai_generate(ctx)
        ids = res.output["article_ids"]
        assert len(ids) == 3 and res.output["errors"] == []
        gid = res.output["group_id"]
        assert gid is not None
        with app.session_factory() as db:
            g = db.get(ArticleGroup, gid)
            assert g.user_id == uid and g.name.startswith("每日生成 · ")
            items = db.query(ArticleGroupItem).filter(ArticleGroupItem.group_id == gid).all()
            assert {it.article_id for it in items} == set(ids)  # 三篇都进组
            for aid in ids:
                assert db.get(Article, aid).review_status == "pending"  # 都标待审
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_flat_off_emits_no_group_id_and_creates_no_group(monkeypatch):
    from server.app.modules.articles.models import ArticleGroup

    _patch_generate(monkeypatch)
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_generate_node import run_ai_generate

        uid = _uid(app)
        t = _make_tpl(app, uid)
        ctx = _ctx(
            app, uid,
            {"prompt_template_id": t, "count": 2, "model": None},  # 无 daily_group
            {"question_text": "q"},
        )
        res = run_ai_generate(ctx)
        assert len(res.output["article_ids"]) == 2
        assert res.output.get("group_id") is None  # 关闭 → 不输出 group_id
        with app.session_factory() as db:
            cnt = db.query(ArticleGroup).filter(
                ArticleGroup.user_id == uid, ArticleGroup.is_deleted == False  # noqa: E712
            ).count()
            assert cnt == 0  # 旧行为：本节点不建组（留给 to_review/执行器）
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_units_partial_failure_keeps_succeeded_in_group(monkeypatch):
    from server.app.modules.articles.models import ArticleGroupItem

    _patch_generate(monkeypatch)
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_generate_node import run_ai_generate

        uid = _uid(app)
        t_ok = _make_tpl(app, uid)
        units = [
            {"question_type": "A", "question_text": "1. qa",
             "allowed_prompt_template_ids": [t_ok], "article_count": 1},   # 成功
            {"question_type": "B", "question_text": "1. qb",
             "allowed_prompt_template_ids": [], "article_count": 1},        # 无模板 → 失败
        ]
        ctx = _ctx(
            app, uid,
            {"prompt_template_id": None, "count": 1, "model": None, "daily_group": True},
            {"generation_units": units},
        )
        res = run_ai_generate(ctx)
        assert len(res.output["article_ids"]) == 1   # 只有 A 成功
        assert len(res.output["errors"]) == 1        # B 记错、不抛
        gid = res.output["group_id"]
        with app.session_factory() as db:
            items = db.query(ArticleGroupItem).filter(ArticleGroupItem.group_id == gid).all()
            assert {it.article_id for it in items} == set(res.output["article_ids"])  # 仅成功篇进组
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_degrades_to_non_streaming_when_resolve_fails(monkeypatch):
    from server.app.modules.articles.models import ArticleGroup

    _patch_generate(monkeypatch)
    # 建组失败 → 退回非流式：仍生成、不输出 group_id、不建组
    monkeypatch.setattr(
        "server.app.modules.articles.service.resolve_or_create_daily_group",
        lambda *a, **k: None,
    )
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_generate_node import run_ai_generate

        uid = _uid(app)
        t = _make_tpl(app, uid)
        ctx = _ctx(
            app, uid,
            {"prompt_template_id": t, "count": 2, "model": None, "daily_group": True},
            {"question_text": "q"},
        )
        res = run_ai_generate(ctx)
        assert len(res.output["article_ids"]) == 2          # 不丢文章
        assert res.output.get("group_id") is None            # 降级 → 无 group_id
        with app.session_factory() as db:
            cnt = db.query(ArticleGroup).filter(
                ArticleGroup.user_id == uid, ArticleGroup.is_deleted == False  # noqa: E712
            ).count()
            assert cnt == 0
    finally:
        app.cleanup()
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `GEO_TEST_DATABASE_URL=... pytest server/tests/test_streaming_daily_group.py -q -k "flat or units_partial or degrades"`
Expected: FAIL — `KeyError: 'group_id'` 或 group_id 非空断言失败（流式逻辑还没接）。

- [ ] **Step 3: 实现 `_make_group_streamer` 并接入两路径**

3a. `server/app/modules/pipelines/nodes/ai_generate_node.py` 顶部 import 区（现有 [:9-15](../../../server/app/modules/pipelines/nodes/ai_generate_node.py)）补充：

```python
import datetime as dt
import itertools
import threading
from zoneinfo import ZoneInfo
```

3b. 在模块内（`run_ai_generate` 之前）新增 helper：

```python
def _make_group_streamer(ctx, cfg):
    """daily_group 开启时先建好当天分组，返回 (group_id, stream_fn)；
    关闭或建组失败 → 返回 (None, no-op)（退回非流式老路径，不丢文章）。

    stream_fn(aid)：把该篇标 pending + 追加进当天组。sort_order 用进程内计数器
    （threading.Lock 只护内存自增；DB 追加在锁外并发、各自不同行，无 DB 锁竞争——见 spec 第 7 节）。"""
    if not cfg.get("daily_group"):
        return None, (lambda _aid: None)

    from server.app.core.config import get_settings
    from server.app.modules.articles.service import (
        append_article_to_group_pending,
        resolve_or_create_daily_group,
    )

    today = dt.datetime.now(ZoneInfo(get_settings().scheduler_tz)).date()
    group_name = f"每日生成 · {today:%Y-%m-%d}"
    resolved = resolve_or_create_daily_group(
        ctx.session_factory, user_id=ctx.user_id, group_name=group_name
    )
    if resolved is None:
        logger.warning("ai_generate daily_group 建组失败，退回非流式：%s", group_name)
        return None, (lambda _aid: None)

    group_id, next_start = resolved
    counter = itertools.count(next_start)
    lock = threading.Lock()

    def _stream(aid: int) -> None:
        with lock:
            so = next(counter)
        append_article_to_group_pending(
            ctx.session_factory, group_id=group_id, article_id=aid, sort_order=so
        )

    return group_id, _stream
```

3c. 改 `_run_units`：在 `total` 校验之后（现 [:70-71](../../../server/app/modules/pipelines/nodes/ai_generate_node.py) 的 `if total > max_count` 之后）、`article_ids: list[int] = []` 之前插入建组；在内嵌 `_one` 的 `return` 前调 `stream`；最后 return 的 output 加 `group_id`：

```python
    group_id, stream = _make_group_streamer(ctx, cfg)

    article_ids: list[int] = []
    errors: list[str] = []

    def _one(qtext: str, tpl_ids: list[int]) -> int:
        db = ctx.session_factory()
        try:
            tpl = _pick_valid_template(db, tpl_ids, ctx.user_id) if tpl_ids else None
            if tpl is None:
                raise ValidationError("该单元允许模板在运行时全部无效或未配置")
            template_content = tpl.content
        finally:
            db.close()
        aid = generate_article_from_prompt(
            session_factory=ctx.session_factory,
            user_id=ctx.user_id,
            template_content=template_content,
            question_text=qtext,
            model=model,
        )
        stream(aid)
        return aid
```

`_run_units` 的 return（现 [:105-107](../../../server/app/modules/pipelines/nodes/ai_generate_node.py)）改为：

```python
    return NodeResult(
        output={"article_ids": article_ids, "errors": errors, "group_id": group_id},
        article_ids=article_ids,
    )
```

3d. 改扁平路径：在 `template_content = tpl.content` 之后（现 [:156](../../../server/app/modules/pipelines/nodes/ai_generate_node.py)）、`article_ids: list[int] = []` 之前插入建组；`_one` 加 `stream`；return 加 `group_id`：

```python
    group_id, stream = _make_group_streamer(ctx, cfg)

    article_ids: list[int] = []
    errors: list[str] = []

    def _one() -> int:
        aid = generate_article_from_prompt(
            session_factory=ctx.session_factory,
            user_id=ctx.user_id,
            template_content=template_content,
            question_text=question_text,
            model=model,
        )
        stream(aid)
        return aid

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_one) for _ in range(count)]
        for fut in as_completed(futures):
            try:
                article_ids.append(fut.result())
            except Exception as exc:
                errors.append(str(exc))

    return NodeResult(
        output={"article_ids": article_ids, "errors": errors, "group_id": group_id},
        article_ids=article_ids,
    )
```

- [ ] **Step 4: 跑测试，确认通过**

Run: `GEO_TEST_DATABASE_URL=... pytest server/tests/test_streaming_daily_group.py -q`
Expected: PASS（Task 1 + Task 2 全过）。

- [ ] **Step 5: 回归 —— 确认旧 ai_generate 用例不受影响**

Run: `GEO_TEST_DATABASE_URL=... pytest server/tests/test_ai_generate_units.py -q`
Expected: PASS（输出新增 `group_id` 键不影响旧断言；它们只查 `article_ids`/`errors`）。

- [ ] **Step 6: 提交**

```bash
git add server/app/modules/pipelines/nodes/ai_generate_node.py server/tests/test_streaming_daily_group.py
git commit -m "feat(pipelines): ai_generate 支持边生成边流式进每日分组"
```

---

## Task 3: ai_generate 配置加 daily_group toggle（零前端）

**Files:**
- Modify: `server/app/modules/pipelines/router.py:86-94`
- Test: `server/tests/test_streaming_daily_group.py`（追加 node-types 用例）

- [ ] **Step 1: 写失败测试**

```python
# ---- Task 3: config_schema toggle ----
@pytest.mark.mysql
def test_node_types_ai_generate_has_daily_group_toggle(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        r = app.client.get("/api/pipelines/node-types")
        assert r.status_code == 200, r.text
        types = {nt["type"]: nt for nt in r.json()["node_types"]}
        fields = {f["key"]: f for f in types["ai_generate"]["config_schema"]}
        assert "daily_group" in fields
        assert fields["daily_group"]["type"] == "toggle"
        assert fields["daily_group"].get("default") is False
    finally:
        app.cleanup()
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `GEO_TEST_DATABASE_URL=... pytest server/tests/test_streaming_daily_group.py::test_node_types_ai_generate_has_daily_group_toggle -q`
Expected: FAIL — `KeyError: 'daily_group'`。

- [ ] **Step 3: 给 ai_generate 的 config_schema 加 toggle**

`server/app/modules/pipelines/router.py` 的 ai_generate 段（[:86-94](../../../server/app/modules/pipelines/router.py)），在 `model` 字段后加一项：

```python
            {
                "type": "ai_generate",
                "label": "AI 生文",
                "config_schema": [
                    {"key": "prompt_template_id", "type": "prompt_template", "label": "提示词模板"},
                    {"key": "count", "type": "number", "label": "生成数量"},
                    {"key": "model", "type": "ai_engine", "label": "模型"},
                    {
                        "key": "daily_group",
                        "type": "toggle",
                        "label": "边生成边进每日分组",
                        "hint": "开启后：生成前先建好「每日生成 · 日期」分组，每生成一篇立即进组并标待审；"
                        "运行中可实时看到逐篇进组，中途失败也不丢已生成的文章。同一天多次运行并入同一组。",
                        "default": False,
                    },
                ],
            },
```

- [ ] **Step 4: 跑测试，确认通过**

Run: `GEO_TEST_DATABASE_URL=... pytest server/tests/test_streaming_daily_group.py::test_node_types_ai_generate_has_daily_group_toggle -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/pipelines/router.py server/tests/test_streaming_daily_group.py
git commit -m "feat(pipelines): ai_generate 节点配置加 daily_group 开关"
```

---

## Task 4: to_review 守卫（上游已成组就透传，防双重成组）

**Files:**
- Modify: `server/app/modules/pipelines/nodes/to_review.py:17`（`run_to_review` 开头）
- Test: `server/tests/test_streaming_daily_group.py`（追加用例）

- [ ] **Step 1: 写失败测试**

```python
# ---- Task 4: to_review 守卫 ----
@pytest.mark.mysql
def test_to_review_passthrough_when_already_grouped(monkeypatch):
    from server.app.modules.articles.models import ArticleGroup
    from server.app.modules.pipelines.nodes.to_review import run_to_review

    app = build_test_app(monkeypatch)
    try:
        uid = _uid(app)
        a1, a2 = _make_article(app.client, "甲"), _make_article(app.client, "乙")
        # 上游已带 group_id（模拟 ai_generate 流式成组）+ to_review daily_group=关
        ctx = _ctx(app, uid, {"daily_group": False},
                   {"article_ids": [a1, a2], "group_id": 4242})
        res = run_to_review(ctx)
        assert res.output["group_id"] == 4242            # 原样透传
        assert res.output["article_ids"] == [a1, a2]
        with app.session_factory() as db:               # 没另建任何组
            cnt = db.query(ArticleGroup).filter(
                ArticleGroup.user_id == uid, ArticleGroup.is_deleted == False  # noqa: E712
            ).count()
            assert cnt == 0
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_to_review_guard_reads_group_id_from_upstream(monkeypatch):
    """inputMapping 把 group_id 从 inputs 筛掉时，守卫仍能从 upstream 兜底取回。"""
    from server.app.modules.pipelines.nodes.to_review import run_to_review

    app = build_test_app(monkeypatch)
    try:
        uid = _uid(app)
        a1 = _make_article(app.client, "甲")
        ctx = _ctx(app, uid, {"daily_group": False},
                   {"article_ids": [a1]}, upstream={"group_id": 777})
        res = run_to_review(ctx)
        assert res.output["group_id"] == 777
    finally:
        app.cleanup()
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `GEO_TEST_DATABASE_URL=... pytest server/tests/test_streaming_daily_group.py -q -k to_review`
Expected: FAIL — 守卫还没加，`run_to_review` 会用 `mark_pending_and_group` 另建一个组（`group_id` ≠ 4242，且组数 == 1 而非 0）。

- [ ] **Step 3: 加守卫**

`server/app/modules/pipelines/nodes/to_review.py`，在 `run_to_review` 取到 `article_ids` 的空判之后（现 [:19-21](../../../server/app/modules/pipelines/nodes/to_review.py)）、`if cfg.get("daily_group")` 之前插入：

```python
def run_to_review(ctx: NodeRunContext) -> NodeResult:
    cfg = ctx.config or {}
    article_ids = ctx.inputs.get("article_ids") or cfg.get("article_ids") or []
    if not article_ids:
        return NodeResult(output={"skipped": "无文章"}, article_ids=[])

    # 守卫：上游已带 group_id（ai_generate 已流式成组）→ 透传，不再建新组。
    # 同查 inputs 与 upstream（防下游 inputMapping 把 group_id 字段筛掉）。
    already_gid = ctx.inputs.get("group_id") or (ctx.upstream or {}).get("group_id")
    if already_gid:
        return NodeResult(
            output={"group_id": already_gid, "article_ids": list(article_ids)},
            article_ids=[],
        )

    if cfg.get("daily_group"):
        # ...（以下维持现状不动）...
```

- [ ] **Step 4: 跑测试，确认通过**

Run: `GEO_TEST_DATABASE_URL=... pytest server/tests/test_streaming_daily_group.py -q -k to_review`
Expected: PASS。

- [ ] **Step 5: 回归 —— 确认 to_review 旧行为不变**

Run: `GEO_TEST_DATABASE_URL=... pytest server/tests/test_daily_grouping.py -q`
Expected: PASS（无 group_id 上游时守卫不触发，daily/普通分支照旧）。

- [ ] **Step 6: 提交**

```bash
git add server/app/modules/pipelines/nodes/to_review.py server/tests/test_streaming_daily_group.py
git commit -m "fix(pipelines): to_review 上游已成组时透传，避免与 ai_generate 流式双重成组"
```

---

## Task 5: 文档 + 全门禁

**Files:**
- Modify: `CLAUDE.md`（pipelines 模块段）

- [ ] **Step 1: 更新 CLAUDE.md**

在 `pipelines/` 模块段落里描述节点的那段（提到 `ai_generate` / `to_review` 的 `daily_group` 处），补一句：

```
ai_generate 增 `daily_group` 开关（默认关）：开启则生成前先建好「每日生成 · 日期」分组、每篇生成成功立即流式进组并标待审（运行中可实时观察、中途失败不丢已生成的）；输出 group_id 后 to_review 靠「上游已带 group_id 就透传」守卫让位，executor 不再兜底成组。并发安全：组提前建一次、append 不动组行、sort_order 用进程内计数器（不走 DB FOR UPDATE）。
```

- [ ] **Step 2: 跑全套后端门禁**

```bash
ruff check server/
ruff format --check server/
mypy server/app
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test pytest server/tests/ -q
```

Expected: 全绿。若 `ruff format --check` 报格式，跑 `ruff format server/` 后重新 `git add` 并并入下一个提交。

- [ ] **Step 3: 提交**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md 记 ai_generate daily_group 流式进组"
```

---

## Self-Review（计划自检结果）

**Spec coverage（逐节对照）：**
- spec §5.1 两个 service helper → Task 1 ✅
- spec §5.2 ai_generate flat+units 流式 + 输出 group_id + 建组失败降级 → Task 2 ✅
- spec §5.3 to_review 守卫（同查 inputs/upstream）→ Task 4 ✅
- spec §5.4 config_schema toggle → Task 3 ✅
- spec §6 交互矩阵：`开+关` 双重成组防护 → Task 4 `test_to_review_passthrough_when_already_grouped`；`关` 旧行为 → Task 2 `test_flat_off_*` + Task 4 Step 5 回归 ✅
- spec §7 死锁规避（组提前建、append 不动组行、sort_order 内存计数器、resolve catch OperationalError）→ Task 1（`test_append_..._leaves_group_row_untouched`、`test_resolve_reuses_after_concurrent_create`）+ Task 2（`_make_group_streamer` lock/counter）✅
- spec §8 建组失败降级 → Task 2 `test_degrades_to_non_streaming_when_resolve_fails` ✅；全失败留空组 = 「count>0 才建组」的自然结果（Task 2 建组在 total/count 校验之后），无需额外代码。
- spec §9 测试计划 → Task 1-4 全部覆盖；node-types → Task 3 ✅

**Placeholder scan：** 无 TBD/TODO；每个代码步骤都给了完整可粘贴代码。

**Type/命名一致性：** `resolve_or_create_daily_group` 返回 `(group_id, next_start)` 二元组，Task 2 `_make_group_streamer` 按 `group_id, next_start = resolved` 解构一致；`append_article_to_group_pending(session_factory, *, group_id, article_id, sort_order)` 签名在 Task 1 定义、Task 2 `_stream` 调用一致；`stream(aid)` 在两路径 `_one` 内调用一致；节点输出键统一 `{"article_ids", "errors", "group_id"}`。

**已知取舍（spec 已记，非缺陷）：** 两 run 同日并发填同一组时 sort_order 可能并列（非唯一约束、合法，仅影响展示排序）。
