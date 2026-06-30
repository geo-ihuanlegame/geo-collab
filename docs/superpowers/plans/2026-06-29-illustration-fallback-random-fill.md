# 配图兜底（检查 + 随机补图）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在共享配图 service `illustrate_one()` 末尾串一个"检查 + 随机补图"兜底步骤，保证该有图的文章不会图太少（不做语义匹配，随机取图补足到 AI 应配张数）。

**Architecture:** 新增 `image_library/fallback.py`（纯逻辑 + 一个写库函数 + 一个 orchestrator）。`illustrate_one()` 配图阶段之后新增「阶段 1.5」串行调用兜底；MCP `ai_illustrate_article` 工具与 web `ai_illustrate` 节点两条路径都经 `illustrate_one`，故一处接入两条全覆盖。补图数经 `IllustrateResult.fallback_inserted` / `AiIllustrateResponse.fallback_inserted` / 节点 output 回传，便于观测。

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy（MySQL only）/ pytest。复用 `selector.pick_image_id`（`func.rand()` 随机取图）、`inserter.insert_images_at_positions`。

## Global Constraints

- MySQL only。需要 DB 的测试用 `@pytest.mark.mysql` + `server.tests.utils.build_test_app(monkeypatch)`，且 `finally` 里 `test_app.cleanup()`。
- 测试**不要**在模块顶层 import 会拉起 `server.app.db.session` 的模块；DB 相关 import 放函数内（懒导入）。
- 兜底全程 **best-effort**：捕获所有异常 + `logger.exception`，**绝不上抛**，不得影响"文章已落库可用"。
- 只回写 `article.content_json` + `article.version`，**不动** `content_html` / `plain_text`（沿用现有插图代码 `inserter.py` 的既定行为）。
- ruff 选 E/F/I/B/UP，line-length=100。`Callable` 从 `collections.abc` 导入（与 `ai_illustrate_svc.py` 一致）。
- 触发目标（已确认）：`target = min(max(requested, 1), max_images)`；`gap = target − 当前正文图数`；`gap > 0` 才补。
- 取图栏目 = `illustrate_one` 已算好的 `candidate_categories`（主推 + 陪衬，结构 `[{id,name,description}]`）；栏目无图 → 静默 no-op。
- 随机补图排除正文已用 `attrs.stockImageId` 去重。
- 范围：仅 `illustrate_one`；**方案运行（scheme_executor）不动**。

## File Structure

| 文件 | 责任 |
|---|---|
| `server/app/modules/image_library/fallback.py` | **新增**。`count_body_images` / `collect_used_stock_image_ids` / `_spread_positions` / `fill_random_images` / `apply_image_fallback` |
| `server/app/modules/articles/ai_illustrate_svc.py` | **改**。`IllustrateResult` 加 `fallback_inserted`；`illustrate_one` 加阶段 1.5 + 总数累加 |
| `server/app/modules/articles/router.py` | **改**。`AiIllustrateResponse` 加 `fallback_inserted`，MCP 端点透传 |
| `server/app/modules/pipelines/nodes/ai_illustrate.py` | **改**。节点 output 汇总 `fallback_inserted`（非破坏性新增） |
| `server/tests/test_illustration_fallback.py` | **新增**。Task 1 纯函数单测 + Task 2 端点集成测 + Task 3 节点单测 |

---

### Task 1: `image_library/fallback.py` — 检查 + 随机补图 helper

**Files:**
- Create: `server/app/modules/image_library/fallback.py`
- Test: `server/tests/test_illustration_fallback.py`

**Interfaces:**
- Consumes（现有，已核实签名）:
  - `selector.ImageQuery(category_ids: list[int]=[], excluded_ids: list[int]=[], ...)`
  - `selector.pick_image_id(query: ImageQuery, db) -> int | None`（`func.rand()` 随机取一张，排除 `excluded_ids`）
  - `selector.fetch_image_by_id(image_id: int, db) -> StockImageRef | None`
  - `inserter.insert_images_at_positions(content_json: dict, image_refs: list[StockImageRef], positions: list[int]) -> dict`
- Produces（后续 Task 2/3 依赖）:
  - `count_body_images(content_json: dict) -> int`
  - `apply_image_fallback(*, article_id: int, requested: int, category_ids: list[int], max_images: int, session_factory: Callable[[], Session]) -> int` —— 返回实际补入张数

- [ ] **Step 1: 写失败测试**

`server/tests/test_illustration_fallback.py`：

```python
"""配图兜底：检查 + 随机补图。Task 1 纯函数单测（无需 MySQL，stub selector）。"""

from __future__ import annotations

from types import SimpleNamespace

from server.app.modules.image_library import fallback as fb
from server.app.modules.image_library.selector import StockImageRef


def _doc(*nodes):
    return {"type": "doc", "content": list(nodes)}


def _para(text):
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def _img(stock_id):
    return {"type": "image", "attrs": {"src": "/x", "stockImageId": stock_id}}


def _ref(image_id):
    return StockImageRef(
        id=image_id, url=f"/u/{image_id}", filename=f"{image_id}.jpg", width=800, height=400
    )


def _seq_pick(pool):
    """返回一个 pick_image_id 替身：从 pool 里给出第一个不在 excluded_ids 的 id。"""

    def _pick(query, db):
        for pid in pool:
            if pid not in query.excluded_ids:
                return pid
        return None

    return _pick


def test_count_body_images():
    assert fb.count_body_images(_doc(_para("a"))) == 0
    assert fb.count_body_images(_doc(_para("a"), _img(1), _img(2))) == 2


def test_collect_used_stock_image_ids():
    assert fb.collect_used_stock_image_ids(_doc(_para("a"), _img(7), _img(9))) == {7, 9}


def test_fill_random_images_inserts_gap(monkeypatch):
    monkeypatch.setattr(fb, "pick_image_id", _seq_pick([101, 102, 103]))
    monkeypatch.setattr(fb, "fetch_image_by_id", lambda i, db: _ref(i))
    article = SimpleNamespace(content_json=_doc(_para("a"), _para("b"), _para("c")), version=1)
    db = SimpleNamespace(commit=lambda: None)
    n = fb.fill_random_images(db, article, category_ids=[5], gap=2)
    assert n == 2
    assert fb.count_body_images(article.content_json) == 2
    assert article.version == 2


def test_fill_random_images_dedups_used(monkeypatch):
    # 正文已含 101；候选只有 101 → 取不到新图 → 0，且不抛异常
    monkeypatch.setattr(fb, "pick_image_id", _seq_pick([101]))
    monkeypatch.setattr(fb, "fetch_image_by_id", lambda i, db: _ref(i))
    article = SimpleNamespace(content_json=_doc(_para("a"), _img(101)), version=1)
    db = SimpleNamespace(commit=lambda: None)
    assert fb.fill_random_images(db, article, category_ids=[5], gap=1) == 0


def test_apply_fallback_fills_to_target(monkeypatch):
    # requested=3, current=1 → target=3 → 补 2
    monkeypatch.setattr(fb, "pick_image_id", _seq_pick([201, 202, 203]))
    monkeypatch.setattr(fb, "fetch_image_by_id", lambda i, db: _ref(i))
    article = SimpleNamespace(
        content_json=_doc(_para("a"), _img(9), _para("b"), _para("c")),
        is_deleted=False,
        version=1,
    )
    db = SimpleNamespace(get=lambda model, _id: article, commit=lambda: None, close=lambda: None)
    n = fb.apply_image_fallback(
        article_id=1, requested=3, category_ids=[5], max_images=12, session_factory=lambda: db
    )
    assert n == 2
    assert fb.count_body_images(article.content_json) == 3


def test_apply_fallback_noop_when_enough():
    # requested=2, current=3 → target=2, gap<0 → 0
    article = SimpleNamespace(
        content_json=_doc(_img(1), _img(2), _img(3)), is_deleted=False, version=1
    )
    db = SimpleNamespace(get=lambda m, i: article, commit=lambda: None, close=lambda: None)
    n = fb.apply_image_fallback(
        article_id=1, requested=2, category_ids=[5], max_images=12, session_factory=lambda: db
    )
    assert n == 0


def test_apply_fallback_noop_when_no_categories():
    assert (
        fb.apply_image_fallback(
            article_id=1, requested=3, category_ids=[], max_images=12, session_factory=lambda: None
        )
        == 0
    )
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_illustration_fallback.py -q`
Expected: FAIL —— `ModuleNotFoundError: ... image_library.fallback` 或 `AttributeError`（模块还没建）。

- [ ] **Step 3: 写最小实现**

`server/app/modules/image_library/fallback.py`：

```python
"""配图兜底：检查正文图数、不足时从图库随机补图（不做语义匹配）。

挂在 illustrate_one 配图主流程之后，保证"该有图的文章不会图太少"。
纯逻辑函数 + 一个写库函数 + 一个 orchestrator；全部 best-effort，调用方负责吞异常。
只回写 content_json + version（沿用 inserter.py 既定行为，不动 content_html/plain_text）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from server.app.modules.image_library.inserter import insert_images_at_positions
from server.app.modules.image_library.selector import (
    ImageQuery,
    fetch_image_by_id,
    pick_image_id,
)

_logger = logging.getLogger(__name__)


def count_body_images(content_json: dict) -> int:
    """数 Tiptap 顶层 content 数组里 type==image 的节点数。"""
    return sum(
        1
        for node in (content_json.get("content") or [])
        if isinstance(node, dict) and node.get("type") == "image"
    )


def collect_used_stock_image_ids(content_json: dict) -> set[int]:
    """收集正文已用的 stockImageId，用于补图去重。"""
    used: set[int] = set()
    for node in content_json.get("content") or []:
        if isinstance(node, dict) and node.get("type") == "image":
            sid = (node.get("attrs") or {}).get("stockImageId")
            if isinstance(sid, int):
                used.add(sid)
    return used


def _spread_positions(content_json: dict, n: int) -> list[int]:
    """在正文顶层块里均匀挑 n 个插入位（跳过 image 节点本身、跳过紧邻已有 image 的位置）。

    返回的下标语义同 inserter.insert_images_at_positions：表示"在该节点之后插入"。
    候选不足 n 个时返回实际候选；完全没有候选时退化为末尾。
    """
    nodes = content_json.get("content") or []
    total = len(nodes)
    if total == 0 or n <= 0:
        return []
    candidates: list[int] = []
    for i, node in enumerate(nodes):
        if not isinstance(node, dict) or node.get("type") == "image":
            continue
        nxt = nodes[i + 1] if i + 1 < total else None
        if isinstance(nxt, dict) and nxt.get("type") == "image":
            continue
        candidates.append(i)
    if not candidates:
        return [total - 1]
    if n >= len(candidates):
        return candidates
    step = len(candidates) / n
    return [candidates[int(k * step)] for k in range(n)]


def fill_random_images(db: Session, article: Any, *, category_ids: list[int], gap: int) -> int:
    """从 category_ids 随机取 gap 张图（排除正文已用），均匀插入正文。返回实际补入张数。

    best-effort：候选不足时按实际数量补；一张都取不到则返回 0、不改文档。
    """
    content = article.content_json or {}
    excluded = list(collect_used_stock_image_ids(content))
    refs = []
    for _ in range(max(0, gap)):
        img_id = pick_image_id(ImageQuery(category_ids=category_ids, excluded_ids=excluded), db)
        if img_id is None:
            break
        excluded.append(img_id)
        ref = fetch_image_by_id(img_id, db)
        if ref is not None:
            refs.append(ref)
    if not refs:
        return 0
    positions = _spread_positions(content, len(refs))
    article.content_json = insert_images_at_positions(content, refs, positions)
    article.version = (article.version or 0) + 1
    db.commit()
    return len(refs)


def apply_image_fallback(
    *,
    article_id: int,
    requested: int,
    category_ids: list[int],
    max_images: int,
    session_factory: Callable[[], Session],
) -> int:
    """兜底 orchestrator：开独立短 session，按 target 规则决定缺口并随机补足。返回补入张数。

    target = min(max(requested, 1), max_images)；gap = target − 当前正文图数；gap>0 才补。
    """
    if not category_ids:
        return 0
    from server.app.modules.articles.models import Article

    db = session_factory()
    try:
        article = db.get(Article, article_id)
        if article is None or getattr(article, "is_deleted", False):
            return 0
        content = article.content_json or {}
        current = count_body_images(content)
        target = min(max(requested, 1), max_images)
        gap = target - current
        if gap <= 0:
            return 0
        return fill_random_images(db, article, category_ids=category_ids, gap=gap)
    finally:
        db.close()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_illustration_fallback.py -q`
Expected: PASS（7 个用例全绿）。

- [ ] **Step 5: lint**

Run: `ruff check server/app/modules/image_library/fallback.py server/tests/test_illustration_fallback.py`
Expected: 无 error（无未用 import；`Callable` 来自 `collections.abc`）。

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/image_library/fallback.py server/tests/test_illustration_fallback.py
git commit -m "feat(image_library): add illustration fallback random-fill helpers"
```

---

### Task 2: 接入 `illustrate_one` + 结果回传 + MCP 端点字段

**Files:**
- Modify: `server/app/modules/articles/ai_illustrate_svc.py`（`IllustrateResult` 加字段；`illustrate_one` 加阶段 1.5 + 总数累加）
- Modify: `server/app/modules/articles/router.py:1061-1073`（`AiIllustrateResponse` 加 `fallback_inserted`）+ `:1122-1131`（端点透传）
- Test: `server/tests/test_illustration_fallback.py`（追加端点集成测）

**Interfaces:**
- Consumes: `fallback.apply_image_fallback(...)`（Task 1）
- Produces: `IllustrateResult.fallback_inserted: int`、`AiIllustrateResponse.fallback_inserted: int`

- [ ] **Step 1: 写失败的集成测试（追加到测试文件末尾）**

```python
import pytest


@pytest.mark.mysql
def test_illustrate_one_fills_missed_via_endpoint(monkeypatch):
    """run_ai_format 只配 1 张但 requested=3 → 兜底随机补 2 张，端点回传 fallback_inserted=2。"""
    from server.app.modules.articles.models import Article
    from server.app.modules.image_library.models import StockCategory, StockImage
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        monkeypatch.setenv("GEO_MCP_TOKEN", "secret")
        from server.app.core import config

        config.get_settings.cache_clear()

        # 1) 建主推栏目 + 3 张图（pick_image_id 只查 id，不需要真实文件）
        with test_app.session_factory() as db:
            cat = StockCategory(name="主推-test", bucket_name="b-main-test", kind="main")
            db.add(cat)
            db.flush()
            for i in range(3):
                db.add(
                    StockImage(
                        category_id=cat.id,
                        minio_key=f"k-fallback-{i}",
                        filename=f"img{i}.jpg",
                        width=800,
                        height=400,
                    )
                )
            # 2) 建一篇带 ai_format target（heading）的文章
            article = Article(
                user_id=test_app.admin_id,
                title="十大新游盘点",
                content_json={
                    "type": "doc",
                    "content": [
                        {"type": "heading", "attrs": {"level": 2},
                         "content": [{"type": "text", "text": "游戏一、A"}]},
                        {"type": "paragraph", "content": [{"type": "text", "text": "正文一"}]},
                        {"type": "paragraph", "content": [{"type": "text", "text": "正文二"}]},
                    ],
                },
                content_html="<h2>游戏一、A</h2><p>正文一</p><p>正文二</p>",
                plain_text="游戏一、A 正文一 正文二",
                version=1,
            )
            db.add(article)
            db.commit()
            cat_id = cat.id
            article_id = article.id

        # 3) 替身 run_ai_format：插 1 张图、写诊断 requested=3/inserted=1、返回 1
        def fake_run_ai_format(article_id_, **kwargs):
            with test_app.session_factory() as db:
                art = db.get(Article, article_id_)
                content = art.content_json or {}
                nodes = list(content.get("content") or [])
                nodes.insert(1, {"type": "image", "attrs": {"src": "/x", "stockImageId": None}})
                art.content_json = {**content, "content": nodes}
                art.version = (art.version or 0) + 1
                db.commit()
            out = kwargs.get("out_diagnostics")
            if out is not None:
                out.update({"requested": 3, "inserted": 1, "missed": 2, "missed_games": ["B", "C"]})
            return 1

        monkeypatch.setattr(
            "server.app.modules.articles.ai_illustrate_svc.run_ai_format", fake_run_ai_format
        )

        r = test_app.client.post(
            f"/api/articles/{article_id}/ai-illustrate",
            json={"main_category_id": cat_id, "set_cover": False},
            headers={"X-MCP-Token": "secret"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["fallback_inserted"] == 2
        assert body["images_inserted"] == 3  # 1（run_ai_format）+ 2（兜底）

        with test_app.session_factory() as db:
            art = db.get(Article, article_id)
            img_count = sum(
                1
                for n in art.content_json["content"]
                if isinstance(n, dict) and n.get("type") == "image"
            )
            assert img_count == 3
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test python -m pytest server/tests/test_illustration_fallback.py::test_illustrate_one_fills_missed_via_endpoint -q`
Expected: FAIL —— `KeyError: 'fallback_inserted'`（响应里还没这个字段），或 `images_inserted==1`（还没接兜底）。

> 本机跑测试用 `env python` 全路径或激活 conda（见记忆 run-tests-env）；`GEO_TEST_DATABASE_URL` 指向共享 LAN 测试库。

- [ ] **Step 3: `IllustrateResult` 加字段**

`ai_illustrate_svc.py`，在 `IllustrateResult` 末尾（`missed_games` 之后）加：

```python
    # 兜底随机补图张数（独立于 AI 语义配图，供观测：哪几张是随机补的）
    fallback_inserted: int = 0
```

- [ ] **Step 4: `illustrate_one` 顶部加 import**

`ai_illustrate_svc.py` 的 import 区，加：

```python
from server.app.modules.image_library.fallback import apply_image_fallback
```

- [ ] **Step 5: 加阶段 1.5（run_ai_format 之后、封面之前）**

在 `images_inserted = run_ai_format(...)`（含 `run_ai_format_from_game_list` 两分支）那段**之后**、`# 阶段 2: 封面` 之前，插入：

```python
    # 阶段 1.5: 随机补图兜底（best-effort，绝不影响配图主结果）
    fallback_inserted = 0
    try:
        requested = int(fmt_diag.get("requested", 0) or 0)
        category_ids = [
            c["id"] for c in candidate_categories if isinstance(c, dict) and c.get("id")
        ]
        fallback_inserted = apply_image_fallback(
            article_id=article_id,
            requested=requested,
            category_ids=category_ids,
            max_images=max_images,
            session_factory=session_factory,
        )
    except Exception:  # noqa: BLE001 — 兜底失败不能拖垮"文章已落库可用"
        _logger.exception("fallback random fill failed for article %s", article_id)
```

- [ ] **Step 6: 阶段 3 用总数 + 回填新字段**

把 `_resolve_illustration_outcome(...)` 调用与最终 `return IllustrateResult(...)` 改成累加总数。
现状（阶段 3）：

```python
    format_error, warning, requested, missed, missed_games = _resolve_illustration_outcome(
        raw_error=raw_error,
        images_inserted=images_inserted or 0,
        fmt_diag=fmt_diag,
    )

    return IllustrateResult(
        article_id=article_id,
        images_inserted=images_inserted or 0,
        cover_status=cover_status,
        cover_error=cover_error,
        format_error=format_error,
        warning=warning,
        requested=requested,
        missed=missed,
        missed_games=missed_games,
    )
```

改为：

```python
    total_inserted = (images_inserted or 0) + fallback_inserted
    format_error, warning, requested, missed, missed_games = _resolve_illustration_outcome(
        raw_error=raw_error,
        images_inserted=total_inserted,
        fmt_diag=fmt_diag,
    )

    return IllustrateResult(
        article_id=article_id,
        images_inserted=total_inserted,
        cover_status=cover_status,
        cover_error=cover_error,
        format_error=format_error,
        warning=warning,
        requested=requested,
        missed=missed,
        missed_games=missed_games,
        fallback_inserted=fallback_inserted,
    )
```

> 注意：`requested` / `missed` / `warning` 仍反映 ai_format 的**语义配图**结果（partial_images 仍可能报"应配3实配1缺2"）——这是有意的：`fallback_inserted` 单独告诉运营"这几张是随机兜底来的"，不假装精准。把 `images_inserted` 改用 `total_inserted` 传入 `_resolve` 仅为避免兜底已补图时仍误报"no images inserted"。

- [ ] **Step 7: `AiIllustrateResponse` 加字段 + 端点透传**

`router.py`，`AiIllustrateResponse`（`missed_games` 之后）加：

```python
    # 兜底随机补图张数（独立于语义配图，images_inserted 已含此数）
    fallback_inserted: int = 0
```

端点 `ai_illustrate_article_mcp` 的 `return AiIllustrateResponse(...)`，在 `missed_games=result.missed_games,` 之后加：

```python
        fallback_inserted=result.fallback_inserted,
```

- [ ] **Step 8: 跑测试确认通过 + 回归现有配图测试**

Run:
```bash
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test python -m pytest server/tests/test_illustration_fallback.py -q
```
Expected: PASS（Task 1 的 7 个 + 本集成测共 8 个全绿）。

- [ ] **Step 9: lint + typecheck**

Run:
```bash
ruff check server/app/modules/articles/ai_illustrate_svc.py server/app/modules/articles/router.py
mypy server/app/modules/articles/ai_illustrate_svc.py
```
Expected: 无 error。

- [ ] **Step 10: Commit**

```bash
git add server/app/modules/articles/ai_illustrate_svc.py server/app/modules/articles/router.py server/tests/test_illustration_fallback.py
git commit -m "feat(illustrate): random-fill fallback in illustrate_one + expose fallback_inserted"
```

---

### Task 3: web `ai_illustrate` 节点 output 汇总 `fallback_inserted`

**Files:**
- Modify: `server/app/modules/pipelines/nodes/ai_illustrate.py`
- Test: `server/tests/test_illustration_fallback.py`（追加节点单测，无需 MySQL，stub `illustrate_one`）

**Interfaces:**
- Consumes: `IllustrateResult.fallback_inserted`（Task 2）
- Produces: 节点 `NodeResult.output["fallback_inserted"]: int`

- [ ] **Step 1: 写失败的节点单测（追加到测试文件末尾）**

```python
def test_ai_illustrate_node_aggregates_fallback(monkeypatch):
    """节点 output 汇总各篇 fallback_inserted / images_inserted。stub illustrate_one，无需 DB。"""
    from server.app.modules.articles.ai_illustrate_svc import IllustrateResult
    from server.app.modules.pipelines.nodes import ai_illustrate as node_mod
    from server.app.modules.pipelines.nodes.base import NodeRunContext

    returns = {
        1: IllustrateResult(article_id=1, images_inserted=3, fallback_inserted=2, cover_status="skipped"),
        2: IllustrateResult(article_id=2, images_inserted=2, fallback_inserted=1, cover_status="skipped"),
    }
    monkeypatch.setattr(
        node_mod, "illustrate_one", lambda *, article_id, **kw: returns[article_id]
    )

    ctx = NodeRunContext(
        session_factory=lambda: None,
        user_id=1,
        config={"main_category_id": 5},
        inputs={"article_ids": [1, 2]},
        upstream={},
    )
    result = node_mod.run_ai_illustrate(ctx)
    assert result.output["fallback_inserted"] == 3
    assert result.output["images_inserted"] == 5
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_illustration_fallback.py::test_ai_illustrate_node_aggregates_fallback -q`
Expected: FAIL —— `KeyError: 'fallback_inserted'`（节点 output 还没这个键）。

- [ ] **Step 3: 节点累加 + 写进 output**

`ai_illustrate.py:run_ai_illustrate`，在 `images_inserted = 0` 那组累加变量旁加 `fallback_inserted = 0`：

```python
    images_inserted = 0
    fallback_inserted = 0
    covers_set = 0
```

在 `for fut in as_completed(futures):` 循环里、`images_inserted += result.images_inserted` 之后加：

```python
                fallback_inserted += result.fallback_inserted
```

最后 `return NodeResult(output={...})` 的 dict 里，`"images_inserted": images_inserted,` 之后加：

```python
            "fallback_inserted": fallback_inserted,
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_illustration_fallback.py::test_ai_illustrate_node_aggregates_fallback -q`
Expected: PASS。

- [ ] **Step 5: 全量回归 + lint**

Run:
```bash
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test python -m pytest server/tests/test_illustration_fallback.py -q
ruff check server/app/modules/pipelines/nodes/ai_illustrate.py
```
Expected: 9 个用例全绿；ruff 无 error。

- [ ] **Step 6: Commit**

```bash
git add server/app/modules/pipelines/nodes/ai_illustrate.py server/tests/test_illustration_fallback.py
git commit -m "feat(pipelines): aggregate fallback_inserted in ai_illustrate node output"
```

---

## Self-Review

**1. Spec coverage**
- 触发规则（target=min(max(requested,1),max_images)）→ Task 1 `apply_image_fallback` + 测试 `test_apply_fallback_fills_to_target` / `_noop_when_enough`。✅
- 随机取图 + 去重 → Task 1 `fill_random_images` + `test_fill_random_images_dedups_used`。✅
- 接入 `illustrate_one`（覆盖 MCP 工具 + web 配图节点）→ Task 2 阶段 1.5。✅
- 不动 save_article / 方案运行 → 计划未触碰这些文件。✅
- 只写 content_json + version → `fill_random_images` 实现 + Global Constraints。✅
- best-effort 不抛 → 阶段 1.5 try/except + `fill`/`apply` 内无 raise。✅
- 观测字段 `fallback_inserted`（IllustrateResult / AiIllustrateResponse / 节点 output）→ Task 2/3。✅
- 边界：已有图不叠加（`already_has_images` → requested=0 → target=1 → gap≤0）→ `test_apply_fallback_noop_when_enough` 同构覆盖。✅

**2. Placeholder scan:** 无 TBD/TODO；每个 code step 给了完整代码与可跑命令。✅

**3. Type consistency:**
- `apply_image_fallback` / `fill_random_images` / `count_body_images` 签名在 Task 1 定义，Task 2/3 引用一致。✅
- `IllustrateResult.fallback_inserted`（Task 2 定义）被 Task 3 节点测试与实现引用，名一致。✅
- `AiIllustrateResponse.fallback_inserted` 默认 0，端点透传 `result.fallback_inserted`，名一致。✅
- `selector.pick_image_id` / `fetch_image_by_id` / `ImageQuery` / `StockImageRef` 与现有签名核对一致。✅
