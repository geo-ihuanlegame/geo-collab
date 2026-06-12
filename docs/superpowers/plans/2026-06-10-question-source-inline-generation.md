# 问题源节点内联生文（per-type 模板+数量）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让流水线「问题源」节点按「问题类型=最小单元」携带 per-type 允许模板+文章数，AI生文逐单元生成、模板/数量各自独立兜底、模型换 `ai_engine` 下拉并按字段灰显。

**Architecture:** 问题源仍是纯数据节点，在保留扁平 `question_text` 之外新增 `generation_units`（逐类型）；AI生文新增「逐单元」执行路径（有 `generation_units` 时走它，否则保持原扁平行为），复用 scheme 的 `_pick_valid_template` 随机选模板。前端 `QuestionTypePicker` 每张类型卡加「允许模板（多选）+ 文章数」并写 per-type `units`；AI生文面板模型换下拉、模板/数量按上游覆盖度灰显。

**Tech Stack:** FastAPI + SQLAlchemy（后端节点）、React 19 + TS（PipelineEditor）。后端 TDD（MySQL，`build_test_app`）；前端无单测框架，靠 `typecheck` + `build` + 手动验证。

设计稿：`docs/superpowers/specs/2026-06-10-question-source-inline-generation-design.md`

---

## File Structure

- `server/app/modules/pipelines/nodes/question_source.py` — 重写：`_build_units` 把新 `units` / 旧扁平 config 统一成 per-type 单元；输出加 `generation_units`，保留扁平 `question_text`/`question_count`。
- `server/app/modules/pipelines/nodes/ai_generate_node.py` — 重写：新增 `_resolve_units` + `_run_units`（逐单元、字段级兜底、总量上限、失败隔离）；保留原扁平路径。
- `server/app/modules/pipelines/router.py` — `get_node_types` 里 `ai_generate` 的 `model` 字段 `type: "text"` → `type: "ai_engine"`。
- `server/tests/test_question_source_units.py` — 新建：问题源 units 输出 + 闸门 + 整类/精选。
- `server/tests/test_ai_generate_units.py` — 新建：AI生文逐单元 + 兜底 + 上限 + 失败隔离。
- `server/tests/test_pipeline_node_types.py` — 追加：ai_generate model 字段为 ai_engine。
- `web/src/features/pipelines/PipelineEditor.tsx` — `QuestionTypePicker` per-card 模板/数量 + 写 `units`；AI生文面板模板/数量灰显。

约束：后端 service/节点抛命名异常（`ValidationError` 等），不抛裸 `ValueError`（CLAUDE.md）。测试需 `GEO_TEST_DATABASE_URL`、DB 名含 `test`；用 `@pytest.mark.mysql`，`finally: app.cleanup()`。

---

## Task 1: 问题源节点输出 generation_units（保留扁平 + 兼容旧 config）

**Files:**
- Modify (整文件重写): `server/app/modules/pipelines/nodes/question_source.py`
- Test: `server/tests/test_question_source_units.py`（新建）
- 回归: `server/tests/test_question_source_multiselect.py`（不改，须仍通过）

- [ ] **Step 1: 写失败测试** — 新建 `server/tests/test_question_source_units.py`

```python
import pytest

from server.tests.utils import build_test_app


def _make_pool(app, items):
    """items: list[(category, text, source_active)]. record_id='r{i}'. 返回 (pool_id, uid)。"""
    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool
    from server.app.modules.system.models import User

    with app.session_factory() as db:
        uid = db.query(User).first().id
        pool = QuestionPool(user_id=uid, name="池")
        db.add(pool)
        db.flush()
        for i, (cat, text, active) in enumerate(items):
            db.add(QuestionItem(pool_id=pool.id, record_id=f"r{i}", fields={},
                                category=cat, question_text=text, source_active=active))
        db.commit()
        return pool.id, uid


def _run(app, uid, config):
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.question_source import run_question_source

    return run_question_source(NodeRunContext(
        session_factory=app.session_factory, user_id=uid, config=config, inputs={}, upstream={}))


@pytest.mark.mysql
def test_units_emit_per_type_with_tpl_and_count(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        # r0,r1 美食; r2 旅游
        pid, uid = _make_pool(app, [("美食", "红烧肉", True), ("美食", "糖醋", True),
                                    ("旅游", "去哪玩", True)])
        cfg = {"pool_id": pid, "units": [
            {"question_type": "美食", "record_ids": ["r0", "r1"],
             "allowed_prompt_template_ids": [7], "article_count": 2},
            {"question_type": "旅游", "record_ids": None,
             "allowed_prompt_template_ids": [], "article_count": None},
        ]}
        out = _run(app, uid, cfg).output
        gus = out["generation_units"]
        assert len(gus) == 2
        um = {g["question_type"]: g for g in gus}
        assert "红烧肉" in um["美食"]["question_text"] and "糖醋" in um["美食"]["question_text"]
        assert um["美食"]["allowed_prompt_template_ids"] == [7]
        assert um["美食"]["article_count"] == 2
        assert "去哪玩" in um["旅游"]["question_text"]   # record_ids=None → 整类
        assert um["旅游"]["allowed_prompt_template_ids"] == []
        assert um["旅游"]["article_count"] is None
        # 扁平字段保留
        assert out["question_count"] == 3
        assert "红烧肉" in out["question_text"] and "去哪玩" in out["question_text"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_unit_without_questions_is_dropped(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        pid, uid = _make_pool(app, [("美食", "红烧肉", True)])
        cfg = {"pool_id": pid, "units": [
            {"question_type": "美食", "record_ids": ["r0"], "allowed_prompt_template_ids": [1], "article_count": 1},
            {"question_type": "旅游", "record_ids": [], "allowed_prompt_template_ids": [2], "article_count": 5},
        ]}
        out = _run(app, uid, cfg).output
        types = [g["question_type"] for g in out["generation_units"]]
        assert types == ["美食"]          # 旅游无问题 → 弃用
        assert out["question_count"] == 1
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_legacy_config_maps_to_units_by_category(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        pid, uid = _make_pool(app, [("美食", "红烧肉", True), ("旅游", "去哪玩", True), ("科技", "AI", True)])
        out = _run(app, uid, {"pool_id": pid, "question_types": ["美食", "旅游"]}).output
        gus = {g["question_type"]: g for g in out["generation_units"]}
        assert set(gus) == {"美食", "旅游"}
        assert all(g["allowed_prompt_template_ids"] == [] and g["article_count"] is None
                   for g in gus.values())
        assert out["question_count"] == 2
    finally:
        app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest server/tests/test_question_source_units.py -q`
Expected: FAIL（`generation_units` 不存在 / KeyError）。

- [ ] **Step 3: 重写 `question_source.py`**（整文件替换为下面内容）

```python
"""question_source 源节点：按「问题类型 = 最小单元」组织。

每个类型一张卡，配置勾选问题（record_ids，省略/None=整类、自动跟进新同步问题）+ 允许模板 + 文章数。
输出扁平 question_text/question_count（保留给 ai_compose 等只认扁平文本的消费者）
+ generation_units（逐类型、仅含勾了≥1题的类型，供 ai_generate 逐单元生文）。
兼容旧扁平 config（question_types / question_record_ids / question_type）。"""

from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError

UNCATEGORIZED = "__uncategorized__"


def _coerce_count(v) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _build_units(cfg: dict, rows: list[tuple]) -> list[dict]:
    """rows: [(record_id, category, text)]（active、按 id 升序）。
    返回 [{question_type, texts, allowed_prompt_template_ids, article_count}]。"""
    units = cfg.get("units")
    if units is not None:
        out: list[dict] = []
        for u in units:
            if not isinstance(u, dict):
                continue
            qt = u.get("question_type")
            rids = u.get("record_ids")
            if rids is not None:
                rid_set = set(rids)
                texts = [(t or "").strip() for (rid, cat, t) in rows
                         if rid in rid_set and (t or "").strip()]
            elif qt == UNCATEGORIZED:
                texts = [(t or "").strip() for (rid, cat, t) in rows
                         if cat is None and (t or "").strip()]
            else:
                texts = [(t or "").strip() for (rid, cat, t) in rows
                         if cat == qt and (t or "").strip()]
            out.append({
                "question_type": qt,
                "texts": texts,
                "allowed_prompt_template_ids": list(u.get("allowed_prompt_template_ids") or []),
                "article_count": _coerce_count(u.get("article_count")),
            })
        return out

    # 旧扁平 config → 按 category 分组成「无模板/无数量」单元（record_ids > types > 整池）
    question_types = cfg.get("question_types")
    if question_types is None:
        legacy = cfg.get("question_type")
        question_types = [] if (legacy is None or legacy == "") else [legacy]
    record_ids = cfg.get("question_record_ids") or []
    if record_ids:
        rid_set = set(record_ids)
        picked = [(rid, cat, t) for (rid, cat, t) in rows if rid in rid_set]
    elif question_types:
        named = {t for t in question_types if t != UNCATEGORIZED}
        incl_uncat = UNCATEGORIZED in question_types
        picked = [(rid, cat, t) for (rid, cat, t) in rows
                  if cat in named or (incl_uncat and cat is None)]
    else:
        picked = list(rows)

    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for (rid, cat, t) in picked:
        key = cat if cat is not None else UNCATEGORIZED
        if key not in groups:
            groups[key] = []
            order.append(key)
        s = (t or "").strip()
        if s:
            groups[key].append(s)
    return [{"question_type": k, "texts": groups[k],
             "allowed_prompt_template_ids": [], "article_count": None} for k in order]


def run_question_source(ctx: NodeRunContext) -> NodeResult:
    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool

    cfg = ctx.config or {}
    pool_id = cfg.get("pool_id")
    if not pool_id:
        raise ValidationError("question_source 节点需配置 pool_id")

    db = ctx.session_factory()
    try:
        pool = db.get(QuestionPool, pool_id)
        if pool is None or getattr(pool, "is_deleted", False):
            raise ValidationError("问题池不存在")
        rows = (
            db.query(QuestionItem.record_id, QuestionItem.category, QuestionItem.question_text)
            .filter(QuestionItem.pool_id == pool_id, QuestionItem.source_active.is_(True))
            .order_by(QuestionItem.id.asc())
            .all()
        )
    finally:
        db.close()

    rows = [(r[0], r[1], r[2]) for r in rows]
    units = _build_units(cfg, rows)

    gen_units: list[dict] = []
    flat_texts: list[str] = []
    for u in units:
        if not u["texts"]:
            continue  # 闸门：无问题 → 弃用
        rendered = "\n".join(f"{i}. {t}" for i, t in enumerate(u["texts"], start=1))
        gen_units.append({
            "question_type": u["question_type"],
            "question_text": rendered,
            "allowed_prompt_template_ids": u["allowed_prompt_template_ids"],
            "article_count": u["article_count"],
        })
        flat_texts.extend(u["texts"])

    flat_rendered = "\n".join(f"{i}. {t}" for i, t in enumerate(flat_texts, start=1))
    return NodeResult(
        output={
            "question_text": flat_rendered,
            "question_count": len(flat_texts),
            "generation_units": gen_units,
        },
        article_ids=[],
    )


register("question_source", run_question_source)
```

- [ ] **Step 4: 跑新测试 + 回归旧测试**

Run: `pytest server/tests/test_question_source_units.py server/tests/test_question_source_multiselect.py -q`
Expected: 全部 PASS（旧 `test_question_source_multiselect.py` 仍绿，证明扁平输出未回退）。

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/pipelines/nodes/question_source.py server/tests/test_question_source_units.py
git commit -m "feat(pipelines): 问题源节点按类型输出 generation_units（保留扁平+兼容旧config）"
```

---

## Task 2: AI生文节点逐单元路径（字段级兜底 + 上限 + 失败隔离）

**Files:**
- Modify (整文件重写): `server/app/modules/pipelines/nodes/ai_generate_node.py`
- Test: `server/tests/test_ai_generate_units.py`（新建）
- 回归: `server/tests/test_pipeline_template.py`、`server/tests/test_pipeline_logic.py`（不改，须仍通过——证明扁平路径未变）

- [ ] **Step 1: 写失败测试** — 新建 `server/tests/test_ai_generate_units.py`

```python
import uuid

import pytest

from server.tests.utils import build_test_app


def _fake_generate(*, session_factory, user_id, template_content, question_text, model=None):
    from server.app.modules.articles.schemas import ArticleCreate
    from server.app.modules.articles.service import create_article

    db = session_factory()
    try:
        art = create_article(db, user_id, ArticleCreate(
            title=f"A-{uuid.uuid4().hex[:6]}",
            content_json={"type": "doc", "content": []},
            content_html="<p>x</p>", plain_text="x", word_count=1,
            client_request_id=str(uuid.uuid4())))
        db.commit()
        return art.id
    finally:
        db.close()


def _make_tpl(app, uid, enabled=True):
    from server.app.modules.prompt_templates.models import PromptTemplate

    with app.session_factory() as db:
        t = PromptTemplate(name="模板", content="写: {{question}}", scope="generation",
                           user_id=uid, is_enabled=enabled)
        db.add(t)
        db.commit()
        return t.id


def _uid(app):
    from server.app.modules.system.models import User

    with app.session_factory() as db:
        return db.query(User).first().id


def _ctx(app, uid, config, inputs):
    from server.app.modules.pipelines.nodes.base import NodeRunContext

    return NodeRunContext(session_factory=app.session_factory, user_id=uid,
                          config=config, inputs=inputs, upstream={})


@pytest.mark.mysql
def test_units_per_unit_fallback(monkeypatch):
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_generate_node.generate_article_from_prompt",
        _fake_generate)
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_generate_node import run_ai_generate

        uid = _uid(app)
        t_unit, t_fallback = _make_tpl(app, uid), _make_tpl(app, uid)
        units = [
            {"question_type": "A", "question_text": "1. qa",
             "allowed_prompt_template_ids": [t_unit], "article_count": 2},   # 自带模板+数量
            {"question_type": "B", "question_text": "1. qb",
             "allowed_prompt_template_ids": [], "article_count": None},       # 全兜底
        ]
        ctx = _ctx(app, uid, {"prompt_template_id": t_fallback, "count": 3, "model": None},
                   {"generation_units": units})
        res = run_ai_generate(ctx)
        # A: 2 篇；B: 兜底数量 3 篇 → 共 5
        assert len(res.output["article_ids"]) == 5
        assert res.output["errors"] == []
        assert res.article_ids == res.output["article_ids"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_units_total_exceeds_cap_raises(monkeypatch):
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_generate_node.generate_article_from_prompt",
        _fake_generate)
    app = build_test_app(monkeypatch)
    try:
        from server.app.core.config import get_settings
        from server.app.modules.pipelines.nodes.ai_generate_node import run_ai_generate
        from server.app.shared.errors import ValidationError

        uid = _uid(app)
        t = _make_tpl(app, uid)
        cap = get_settings().ai_generate_max_count
        units = [{"question_type": "A", "question_text": "1. q",
                  "allowed_prompt_template_ids": [t], "article_count": cap + 1}]
        ctx = _ctx(app, uid, {"prompt_template_id": t, "count": 1, "model": None},
                   {"generation_units": units})
        with pytest.raises(ValidationError):
            run_ai_generate(ctx)
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_units_missing_template_isolated_to_errors(monkeypatch):
    monkeypatch.setattr(
        "server.app.modules.pipelines.nodes.ai_generate_node.generate_article_from_prompt",
        _fake_generate)
    app = build_test_app(monkeypatch)
    try:
        from server.app.modules.pipelines.nodes.ai_generate_node import run_ai_generate

        uid = _uid(app)
        t_ok = _make_tpl(app, uid)
        units = [
            {"question_type": "A", "question_text": "1. qa",
             "allowed_prompt_template_ids": [t_ok], "article_count": 1},     # 正常
            {"question_type": "B", "question_text": "1. qb",
             "allowed_prompt_template_ids": [], "article_count": 1},          # 无模板且本节点也无兜底模板
        ]
        ctx = _ctx(app, uid, {"prompt_template_id": None, "count": 1, "model": None},
                   {"generation_units": units})
        res = run_ai_generate(ctx)
        assert len(res.output["article_ids"]) == 1   # 只有 A 成功
        assert len(res.output["errors"]) == 1        # B 记错误、不抛
    finally:
        app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest server/tests/test_ai_generate_units.py -q`
Expected: FAIL（逐单元路径未实现，`generation_units` 被忽略 → 走扁平、行为不符）。

- [ ] **Step 3: 重写 `ai_generate_node.py`**（整文件替换）

```python
"""ai_generate 处理节点。

两种模式：
- 逐单元（上游问题源传入 generation_units）：每个单元解析模板/数量（缺则各自兜底到本节点配置），
  每篇从该单元允许模板里随机抽一个有效模板；模型用本节点 ai_engine（config["model"]）。
  总量受 ai_generate_max_count 约束；单篇/单元失败收进 errors，交由运行聚合为 partial_failed。
- 扁平（无 generation_units）：按本节点单模板 + 数量并发生成（原行为）。"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from server.app.modules.ai_generation.article_writer import generate_article_from_prompt
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.modules.prompt_templates.service import get_visible_prompt_template
from server.app.shared.errors import ValidationError


def _resolve_units(units, fallback_template_id, fallback_count) -> list[tuple]:
    """generation_units → [(question_text, template_ids, count)]，模板/数量各自独立兜底。"""
    resolved: list[tuple] = []
    for u in units:
        if not isinstance(u, dict):
            continue
        qtext = (u.get("question_text") or "").strip()
        if not qtext:
            continue
        tpl_ids = list(u.get("allowed_prompt_template_ids") or [])
        if not tpl_ids and fallback_template_id:
            tpl_ids = [fallback_template_id]
        try:
            cnt = int(u.get("article_count"))
        except (TypeError, ValueError):
            cnt = 0
        if cnt <= 0:
            cnt = fallback_count
        resolved.append((qtext, tpl_ids, cnt))
    return resolved


def _run_units(ctx: NodeRunContext, cfg: dict, units, model, max_count) -> NodeResult:
    from server.app.modules.ai_generation.scheme_executor import _pick_valid_template

    fallback_template_id = cfg.get("prompt_template_id")
    fallback_count = int(cfg.get("count") or 0)
    resolved = _resolve_units(units, fallback_template_id, fallback_count)

    total = sum(c for (_, _, c) in resolved)
    if total <= 0:
        raise ValidationError("ai_generate 逐单元：解析后总生成数量为 0（请在问题源或本节点配置数量）")
    if total > max_count:
        raise ValidationError(f"生成数量超过上限 {max_count}")

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
        return generate_article_from_prompt(
            session_factory=ctx.session_factory, user_id=ctx.user_id,
            template_content=template_content, question_text=qtext, model=model)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_one, qtext, tpl_ids)
                   for (qtext, tpl_ids, cnt) in resolved for _ in range(cnt)]
        for fut in as_completed(futures):
            try:
                article_ids.append(fut.result())
            except Exception as exc:  # 单篇失败不中断
                errors.append(str(exc))

    return NodeResult(output={"article_ids": article_ids, "errors": errors}, article_ids=article_ids)


def run_ai_generate(ctx: NodeRunContext) -> NodeResult:
    from server.app.core.config import get_settings

    cfg = ctx.config or {}
    model = cfg.get("model")
    max_count = get_settings().ai_generate_max_count

    units = ctx.inputs.get("generation_units")
    if units:
        return _run_units(ctx, cfg, units, model, max_count)

    # 扁平模式（原行为，未改动语义）
    question_text = ctx.inputs.get("question_text") or cfg.get("question_text") or ""
    if not question_text:
        raise ValidationError("ai_generate 节点缺少 question_text（上游未传且未配置）")

    template_id = cfg.get("prompt_template_id")
    count = int(cfg.get("count") or 0)
    if not template_id or count <= 0:
        raise ValidationError("ai_generate 节点需配置 prompt_template_id 与 count>0")
    if count > max_count:
        raise ValidationError(f"生成数量超过上限 {max_count}")

    db = ctx.session_factory()
    try:
        tpl = get_visible_prompt_template(db, template_id, user_id=ctx.user_id, scope="generation")
        if tpl is None or not tpl.is_enabled:
            raise ValidationError("提示词模板无效（不存在/无权访问/停用/删除/非 generation）")
        template_content = tpl.content
    finally:
        db.close()

    article_ids: list[int] = []
    errors: list[str] = []

    def _one() -> int:
        return generate_article_from_prompt(
            session_factory=ctx.session_factory, user_id=ctx.user_id,
            template_content=template_content, question_text=question_text, model=model)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_one) for _ in range(count)]
        for fut in as_completed(futures):
            try:
                article_ids.append(fut.result())
            except Exception as exc:
                errors.append(str(exc))

    return NodeResult(output={"article_ids": article_ids, "errors": errors}, article_ids=article_ids)


register("ai_generate", run_ai_generate)
```

- [ ] **Step 4: 跑新测试 + 回归扁平路径**

Run: `pytest server/tests/test_ai_generate_units.py server/tests/test_pipeline_template.py server/tests/test_pipeline_logic.py -q`
Expected: 全 PASS（扁平路径回归绿）。

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/pipelines/nodes/ai_generate_node.py server/tests/test_ai_generate_units.py
git commit -m "feat(pipelines): AI生文逐单元生成（字段级兜底+总量上限+失败隔离），保留扁平路径"
```

---

## Task 3: node-types schema —— AI生文「模型」改 ai_engine 下拉

**Files:**
- Modify: `server/app/modules/pipelines/router.py:80`（`ai_generate` 的 `model` 字段）
- Test: `server/tests/test_pipeline_node_types.py`（追加用例）

- [ ] **Step 1: 写失败测试** — 在 `server/tests/test_pipeline_node_types.py` 末尾追加

```python
@pytest.mark.mysql
def test_ai_generate_model_field_is_ai_engine(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        r = app.client.get("/api/pipelines/node-types")
        assert r.status_code == 200, r.text
        types = {nt["type"]: nt for nt in r.json()["node_types"]}
        fields = {f["key"]: f for f in types["ai_generate"]["config_schema"]}
        assert fields["model"]["type"] == "ai_engine"
    finally:
        app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest server/tests/test_pipeline_node_types.py::test_ai_generate_model_field_is_ai_engine -q`
Expected: FAIL（当前 `model` 字段 `type == "text"`）。

- [ ] **Step 3: 改 schema** — `server/app/modules/pipelines/router.py`，把

```python
                    {"key": "model", "type": "text", "label": "模型(可空)"},
```

改为

```python
                    {"key": "model", "type": "ai_engine", "label": "模型"},
```

- [ ] **Step 4: 跑测试确认通过 + 回归 node-types**

Run: `pytest server/tests/test_pipeline_node_types.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/pipelines/router.py server/tests/test_pipeline_node_types.py
git commit -m "feat(pipelines): AI生文节点「模型」字段改 ai_engine 下拉（复用 GEO_AI_ENGINES）"
```

---

## Task 4: 前端 QuestionTypePicker —— per-card 模板/数量 + 写 units

**Files:**
- Modify: `web/src/features/pipelines/PipelineEditor.tsx`（`QuestionTypePicker` 及其辅助函数，约 19-160 行区域）

说明：前端无单测框架，门禁＝`typecheck` + `build` + 手动验证。本任务把 picker 的「最小扁平 config」改为显式 per-type `units`，并在每张卡加「允许模板（多选）」+「文章数」。

数据形状：`config.units: Array<{ question_type: string; record_ids: string[] | null; allowed_prompt_template_ids: number[]; article_count: number | null }>`。`record_ids === null` = 整类（自动跟进）；`[]` = 无勾选（弃用、不写进数组）。

- [ ] **Step 1: 加 units 派生/写回辅助函数** — 在 `QuestionTypePicker` 上方新增下列函数。同时**删除已不再使用的 `deriveQuestionConfig` 函数与 `NONE_SENTINEL` 常量**（避免 tsc `noUnusedLocals` 报错）；**保留** `deriveCheckedRecordIds`（被 `deriveUnitMap` 复用）、`typeSentinel`、`UNCATEGORIZED`。

```typescript
type PickerUnit = {
  question_type: string;
  record_ids: string[] | null;          // null = 整类
  allowed_prompt_template_ids: number[];
  article_count: number | null;
};

// config → 每个类型的 PickerUnit（用于渲染）。优先读新 units；否则从旧扁平 config 推导。
function deriveUnitMap(types: QuestionType[], config: Record<string, unknown>): Map<string, PickerUnit> {
  const map = new Map<string, PickerUnit>();
  const rawUnits = config.units as PickerUnit[] | undefined;
  if (Array.isArray(rawUnits)) {
    for (const t of types) {
      const sent = typeSentinel(t);
      const u = rawUnits.find((x) => x.question_type === sent);
      map.set(sent, u
        ? { question_type: sent,
            record_ids: u.record_ids === null ? null : [...(u.record_ids ?? [])],
            allowed_prompt_template_ids: [...(u.allowed_prompt_template_ids ?? [])],
            article_count: u.article_count ?? null }
        : { question_type: sent, record_ids: [], allowed_prompt_template_ids: [], article_count: null });
    }
    return map;
  }
  // 旧扁平 config → 每类型 record_ids（整类=null，部分=子集，无=[]），无模板/数量
  const checked = deriveCheckedRecordIds(types, config);
  for (const t of types) {
    const sent = typeSentinel(t);
    const rids = t.questions.map((q) => q.record_id);
    const on = rids.filter((r) => checked.has(r));
    const record_ids = on.length === 0 ? [] : on.length === rids.length ? null : on;
    map.set(sent, { question_type: sent, record_ids, allowed_prompt_template_ids: [], article_count: null });
  }
  return map;
}

// PickerUnit map → config.units（只保留「有勾选问题」的类型：record_ids===null 或非空数组）。
function unitsToConfig(map: Map<string, PickerUnit>): Record<string, unknown> {
  const units: PickerUnit[] = [];
  for (const u of map.values()) {
    const included = u.record_ids === null || (Array.isArray(u.record_ids) && u.record_ids.length > 0);
    if (included) units.push(u);
  }
  // 清掉旧扁平字段，统一走 units
  return { units, question_type: undefined, question_types: undefined, question_record_ids: undefined };
}
```

- [ ] **Step 2: 重写 `QuestionTypePicker` 主体** — 用 unit map 驱动渲染，每卡加模板多选 + 数量。把组件签名加 `templates`（生文模板列表）：

```typescript
function QuestionTypePicker({ poolId, types, config, templates, onChange }: {
  poolId: number;
  types: QuestionType[] | undefined;
  config: Record<string, unknown>;
  templates: PromptTemplate[];
  onChange: (patch: Record<string, unknown>) => void;
}) {
  if (!poolId) return <div className="schemeEmpty">请先在上方选择问题池</div>;
  if (types === undefined) return <div className="schemeEmpty">加载问题类型中…</div>;
  if (types.length === 0) {
    return <div className="schemeEmpty">该问题池暂无问题，请先到「AI 生文 · 问题池」同步飞书</div>;
  }

  const unitMap = deriveUnitMap(types, config);
  const commit = (next: Map<string, PickerUnit>) => onChange(unitsToConfig(next));
  const cloneMap = () => new Map([...unitMap].map(([k, v]) => [k, { ...v,
    record_ids: v.record_ids === null ? null : [...v.record_ids],
    allowed_prompt_template_ids: [...v.allowed_prompt_template_ids] }]));

  const checkedSet = (t: QuestionType): Set<string> => {
    const u = unitMap.get(typeSentinel(t))!;
    if (u.record_ids === null) return new Set(t.questions.map((q) => q.record_id)); // 整类=全选
    return new Set(u.record_ids);
  };

  const toggleQuestion = (t: QuestionType, rid: string) => {
    const sent = typeSentinel(t);
    const cur = checkedSet(t);
    if (cur.has(rid)) cur.delete(rid); else cur.add(rid);
    const all = t.questions.map((q) => q.record_id);
    const next = cloneMap();
    const u = next.get(sent)!;
    u.record_ids = cur.size === all.length ? null : [...cur];   // 全选→null(自动跟进)
    commit(next);
  };
  const toggleAll = (t: QuestionType) => {
    const sent = typeSentinel(t);
    const u0 = unitMap.get(sent)!;
    const allOn = u0.record_ids === null;
    const next = cloneMap();
    next.get(sent)!.record_ids = allOn ? [] : null;
    commit(next);
  };
  const removeType = (t: QuestionType) => {          // 排除该类型（清空其勾选）
    const next = cloneMap();
    next.get(typeSentinel(t))!.record_ids = [];
    commit(next);
  };
  const setTemplates = (t: QuestionType, ids: number[]) => {
    const next = cloneMap();
    next.get(typeSentinel(t))!.allowed_prompt_template_ids = ids;
    commit(next);
  };
  const setCount = (t: QuestionType, n: number | null) => {
    const next = cloneMap();
    next.get(typeSentinel(t))!.article_count = n;
    commit(next);
  };

  return (
    <>
      <div className="schemeFieldLabel">
        问题类型 · 共 {types.length} 类（勾选问题=启用该类型；可各自配模板/数量，留空则用 AI 生文兜底）
      </div>
      <div className="schemeLineScroll">
        {types.map((t) => {
          const u = unitMap.get(typeSentinel(t))!;
          const checked = checkedSet(t);
          const checkedCount = t.questions.filter((q) => checked.has(q.record_id)).length;
          const allChecked = checkedCount === t.questions.length && t.questions.length > 0;
          return (
            <div className="schemeLineCard" key={typeSentinel(t)}
              style={{ opacity: checkedCount === 0 ? 0.6 : 1 }}>
              <div className="schemeLineHead">
                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <span className="schemeTypeBadge">{t.question_type ?? "未分类"}</span>
                  <span style={{ fontSize: 12, color: "var(--fg-3)" }}>共 {t.questions.length} 题</span>
                </div>
                <div className="schemeLineActions">
                  <span style={{ color: "var(--fg-3)" }}>已选 {checkedCount} / {t.questions.length}</span>
                  <button type="button" className="schemeLink" onClick={() => toggleAll(t)}>
                    {allChecked ? "取消全选" : "全选"}
                  </button>
                  <button type="button" className="schemeLink"
                    style={{ color: "var(--fg-3)", display: "inline-flex", gap: 4, alignItems: "center" }}
                    onClick={() => removeType(t)} title="排除该问题类型（取消其全部勾选）">
                    <Trash2 size={12} /> 移除
                  </button>
                </div>
              </div>
              <div className="schemeLineSub">
                <span className="schemeSubLabel">选择问题</span>
                <div className="schemeChips">
                  {t.questions.map((q) => {
                    const on = checked.has(q.record_id);
                    const label = (q.question_text || q.record_id || "").trim();
                    return (
                      <button key={q.record_id} type="button"
                        className={`schemeChip${on ? " on" : ""}`} title={label}
                        onClick={() => toggleQuestion(t, q.record_id)}>
                        <span className="schemeChipText">{label}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
              <div className="schemeLineSub">
                <span className="schemeSubLabel">允许模板</span>
                <select className="peMultiSelect" multiple
                  value={u.allowed_prompt_template_ids.map(String)}
                  onChange={(e) => setTemplates(t, Array.from(e.target.selectedOptions, (o) => Number(o.value)))}>
                  {templates.map((tp) => <option key={tp.id} value={tp.id}>{tp.name}</option>)}
                </select>
              </div>
              <div className="schemeLineSub">
                <span className="schemeSubLabel">文章数</span>
                <input type="number" min={1} style={{ width: 80 }}
                  value={u.article_count ?? ""}
                  onChange={(e) => setCount(t, e.target.value ? Number(e.target.value) : null)} />
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}
```

- [ ] **Step 3: 给 picker 传 templates** — 找到渲染 `QuestionTypePicker` 处（约 404 行），加 `templates={genTemplates}`：

```tsx
                      <QuestionTypePicker
                        poolId={poolId}
                        types={poolId ? typesByPool[poolId] : []}
                        config={sel.config}
                        templates={genTemplates}
                        onChange={(patch) =>
                          updateNode(selected!, { config: { ...sel.config, ...patch } })}
                      />
```

- [ ] **Step 4: 处理换池清空** — 找到 `f.type === "question_pool"` 的 onChange（约 419-424 行），把清空字段从旧扁平改为清 `units`：

```tsx
                          updateNode(selected!,
                            { config: { ...sel.config, [f.key]: v, units: undefined,
                              question_types: undefined, question_record_ids: undefined } });
```

- [ ] **Step 5: typecheck + build**

Run: `pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 均通过、无类型错误。

- [ ] **Step 6: 提交**

```bash
git add web/src/features/pipelines/PipelineEditor.tsx
git commit -m "feat(pipelines-web): 问题源类型卡加 允许模板+文章数，写 per-type units"
```

---

## Task 5: 前端 AI生文面板 —— 模型下拉 + 模板/数量按上游覆盖度灰显

**Files:**
- Modify: `web/src/features/pipelines/PipelineEditor.tsx`（字段渲染区，约 359-486 行）

说明：模型字段在 Task 3 已改 `ai_engine`，前端 `ai_engine` 分支（约 429-437 行）会自动渲染下拉，无需额外代码。本任务只加「模板/数量」灰显。

- [ ] **Step 1: 加灰显计算辅助** — 在 `PipelineEditor` 组件内、`return` 之前（紧挨 `selDef` 定义后，约 305 行附近）加：

```typescript
  // AI生文：若上游是 question_source 且其 units 覆盖了模板/数量，则对应字段灰显（已被接管）。
  const aiGenMask = useMemo(() => {
    if (!sel || sel.node_type !== "ai_generate") return { template: false, count: false };
    const dep = sel.flow_meta?.dependsOnIndex;
    const upIdx = dep != null ? dep : (selected != null ? selected - 1 : -1);
    const up = upIdx >= 0 ? nodes[upIdx] : undefined;
    if (!up || up.node_type !== "question_source") return { template: false, count: false };
    const units = (up.config?.units as Array<Record<string, unknown>> | undefined);
    if (!Array.isArray(units) || units.length === 0) return { template: false, count: false };
    const enabled = units.filter((u) => u.record_ids === null ||
      (Array.isArray(u.record_ids) && (u.record_ids as unknown[]).length > 0));
    if (enabled.length === 0) return { template: false, count: false };
    const template = enabled.every((u) => Array.isArray(u.allowed_prompt_template_ids) &&
      (u.allowed_prompt_template_ids as unknown[]).length > 0);
    const count = enabled.every((u) => typeof u.article_count === "number" && (u.article_count as number) > 0);
    return { template, count };
  }, [sel, selected, nodes]);
```

- [ ] **Step 2: 在 ai_generate 的模板/数量字段上接管渲染** — 在 `selDef.config_schema.map((f) => {` 内、`info`/`toggle`/`question_types` 等分支之后、通用 `<label>` 分支之前，加一段对 ai_generate 模板/数量的接管：

```tsx
                // AI生文：模板/数量字段——上游问题源已覆盖时灰显禁用，提示「已接管」
                if (sel.node_type === "ai_generate" && (f.key === "prompt_template_id" || f.key === "count")) {
                  const masked = f.key === "prompt_template_id" ? aiGenMask.template : aiGenMask.count;
                  return (
                    <label className="agentField" key={f.key}>
                      <span className="agentFieldLabel">
                        {f.label}{masked ? "（已由上游问题源接管）" : "（上游未配时的兜底）"}
                      </span>
                      <input
                        type={f.key === "count" ? "number" : "text"}
                        disabled={masked}
                        value={String(sel.config[f.key] ?? "")}
                        onChange={(e) => updateNode(selected!, { config: { ...sel.config,
                          [f.key]: f.key === "count" ? Number(e.target.value) : e.target.value } })} />
                    </label>
                  );
                }
```

注：`prompt_template_id` 沿用现状的文本输入（填模板 ID），本次仅加灰显与提示，不改其渲染形态（超范围）。

- [ ] **Step 3: typecheck + build**

Run: `pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 通过。

- [ ] **Step 4: 提交**

```bash
git add web/src/features/pipelines/PipelineEditor.tsx
git commit -m "feat(pipelines-web): AI生文模型下拉 + 模板/数量按上游覆盖度灰显"
```

---

## Task 6: 端到端验收（后端全测 + 前端门禁 + 手动验证）

**Files:** 无（验收）。

- [ ] **Step 1: 后端 pipelines 相关全测**

Run: `pytest server/tests/test_question_source_units.py server/tests/test_question_source_multiselect.py server/tests/test_ai_generate_units.py server/tests/test_pipeline_node_types.py server/tests/test_pipeline_template.py server/tests/test_pipeline_logic.py server/tests/test_ai_generation_nodes.py -q`
Expected: 全 PASS。

- [ ] **Step 2: 后端 lint/format/type**

Run: `ruff check server/ && ruff format --check server/ && mypy server/app`
Expected: 通过（mypy 宽松）。如 ruff format 报格式，跑 `ruff format server/` 后重提交。

- [ ] **Step 3: 前端门禁**

Run: `pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 通过。

- [ ] **Step 4: 手动验证（需本地起 web+后端，问题池已同步飞书）** 逐条核对：
  - 问题源类型卡：每卡能勾问题、选「允许模板」、填「文章数」；只填模板/数量不勾问题的卡保存后不参与生文。
  - AI生文「模型」是下拉（来自 `GEO_AI_ENGINES`）。
  - 上游问题源每个启用卡都配了模板 → AI生文「模板」字段灰显禁用并标注「已接管」；有卡没配模板 → 保持可编辑、标注「兜底」。数量字段同理独立。
  - 运行：每类型按解析后的数量/模板生成；某类型模板缺失时其它类型仍产出、运行状态 `partial_failed`。

- [ ] **Step 5: 收尾提交（若 Step 2/3 有格式修正）**

```bash
git add -A
git commit -m "chore(pipelines): 问题源内联生文 lint/format 收尾"
```

---

## 验收对照（spec → task）

- 判定表五行（弃用 / 全兜底 / 仅模板 / 仅数量 / 全自带）→ Task 1（弃用、问题源侧）+ Task 2（兜底解析、AI生文侧）+ Task 2 测试覆盖。
- 数据契约 `generation_units` + 保留扁平 `question_text` → Task 1。
- 模板/数量各自独立兜底 → Task 2 `_resolve_units`。
- 模型取 ai_engine + 下拉 → Task 2（读 `config["model"]`）+ Task 3（schema）+ Task 5（前端自动渲染）。
- 字段级灰显 → Task 5。
- 总量上限报错（决策1）/ 单元缺模板跳过（决策2）→ Task 2。
- 整类自动跟进（决策3）/ 旧 config 兼容 → Task 1（`record_ids=None` + legacy 分组）+ Task 4（picker `record_ids=null`）。
- per-card 模板/数量 UI → Task 4。
