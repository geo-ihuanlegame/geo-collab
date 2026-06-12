# 「问题源」多选类型 + 选具体问题 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 把 `question_source` 节点从单选 `question_type` 升级为 `question_types`（多选，含未分类）+ `question_record_ids`（可选精选具体问题），编辑器用两个联动紧凑多选（方案A），向后兼容旧配置。

**Architecture:** 改一个节点 + node-types config_schema + 编辑器三处（缓存结构、两个新字段渲染、移除旧单选渲染）。复用 `/question-pools/{id}/question-types`（已返回分类+问题）。无 DB 迁移（config 是 JSON）。

**Tech Stack:** FastAPI + SQLAlchemy + MySQL + pytest（容器跑）；React 19 + Vite + TS（host pnpm）。

---

## 约定

- **唯一改动目标 = geo-collab**；参考项目只读禁改。
- **后端在容器**：`docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest <args>'`。宿主无 python。`server/` bind-mount。容器内若无 ruff：`pip install ruff -q`。
- **ruff 双门禁**：`ruff check` + `ruff format --check`。
- **前端在 host**：`pnpm --filter @geo/web typecheck` + `build`。
- **分支** `feat/question-source-multiselect`（已基于最新 origin/main 建好，spec 已提交 f849280）。逐 Task 提交。
- 已核实事实（基于本分支）：
  - 现 `question_source.py`：`{pool_id, question_type}`，`question_type` `""`=全部 / `"__uncategorized__"`=未分类 / 具体类。
  - `QuestionItem{id,pool_id,record_id,question_text,category,source_active}`。`/question-pools/{id}/question-types` → `QuestionType[]` = `{question_type, count, questions:[{id,record_id,question_text}]}`。
  - `get_node_types()` question_source 现 config_schema：`[{key:pool_id,type:question_pool}, {key:question_type,type:question_type}]`。
  - `PipelineEditor.tsx`：`typesByPool: Record<number, {value,label}[]>`（line ~31-32）；`ensureTypes`（~54-63）把 QuestionType[] map 成 {value,label}[]；config 渲染三元链中 `f.type === "question_type"`（~231-244）是个 IIFE 单选；多选用 `className="peMultiSelect" multiple`（见 prompt_templates ~254、accounts ~270 分支）。`selected`（节点下标 state）与 `sel`（当前节点）变量名已占用——新分支内**勿用 `selected` 作局部名**。
  - 仅 question_source 用 `question_type` 字段类型；其它节点不用 → 可安全移除该渲染分支。
  - 前端 `QuestionType`/`QuestionBrief` 类型已在 `web/src/types.ts`；`listQuestionTypes(poolId): Promise<QuestionType[]>` 已在 `web/src/api/ai-generation.ts`。

---

## Task 1: 后端节点 + node-types

**Files:**
- Modify: `server/app/modules/pipelines/nodes/question_source.py`（整体替换 `run_question_source`）
- Modify: `server/app/modules/pipelines/router.py`（question_source config_schema）
- Test: `server/tests/test_question_source_multiselect.py`（新建）

- [ ] **Step 1: 写失败测试（@pytest.mark.mysql）**

```python
# server/tests/test_question_source_multiselect.py
import pytest

from server.tests.utils import build_test_app


def _make_pool(app, items):
    """items: list[(category, text, source_active)]. record_id = 'r{i}'. 返回 (pool_id, uid)。"""
    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool
    from server.app.modules.system.models import User

    with app.session_factory() as db:
        uid = db.query(User).first().id
        pool = QuestionPool(user_id=uid, name="池")
        db.add(pool)
        db.flush()
        for i, (cat, text, active) in enumerate(items):
            db.add(QuestionItem(
                pool_id=pool.id, record_id=f"r{i}", fields={},
                category=cat, question_text=text, source_active=active))
        db.commit()
        return pool.id, uid


def _run(app, uid, config):
    from server.app.modules.pipelines.nodes.base import NodeRunContext
    from server.app.modules.pipelines.nodes.question_source import run_question_source

    return run_question_source(NodeRunContext(
        session_factory=app.session_factory, user_id=uid, config=config, inputs={}, upstream={}))


@pytest.mark.mysql
def test_multi_type(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        pid, uid = _make_pool(app, [("美食", "红烧肉", True), ("旅游", "去哪玩", True), ("科技", "AI", True)])
        out = _run(app, uid, {"pool_id": pid, "question_types": ["美食", "旅游"]}).output
        assert out["question_count"] == 2
        assert "红烧肉" in out["question_text"] and "去哪玩" in out["question_text"]
        assert "AI" not in out["question_text"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_types_with_uncategorized(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        pid, uid = _make_pool(app, [("美食", "红烧肉", True), (None, "无分类题", True), ("科技", "AI", True)])
        out = _run(app, uid, {"pool_id": pid, "question_types": ["美食", "__uncategorized__"]}).output
        assert out["question_count"] == 2
        assert "红烧肉" in out["question_text"] and "无分类题" in out["question_text"]
        assert "AI" not in out["question_text"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_record_ids_override_types(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        # r0 美食, r1 旅游
        pid, uid = _make_pool(app, [("美食", "红烧肉", True), ("旅游", "去哪玩", True)])
        out = _run(app, uid, {
            "pool_id": pid, "question_types": ["美食"], "question_record_ids": ["r1"]}).output
        assert out["question_count"] == 1
        assert "去哪玩" in out["question_text"]  # record_ids 优先、忽略类型
        assert "红烧肉" not in out["question_text"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_record_ids_lenient_to_stale(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        # r0 active, r1 inactive
        pid, uid = _make_pool(app, [("美食", "有效题", True), ("美食", "失效题", False)])
        out = _run(app, uid, {
            "pool_id": pid, "question_record_ids": ["r0", "r1", "不存在"]}).output
        assert out["question_count"] == 1  # 只取 active 且存在的
        assert "有效题" in out["question_text"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_legacy_question_type(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        pid, uid = _make_pool(app, [("美食", "红烧肉", True), ("旅游", "去哪玩", True)])
        out = _run(app, uid, {"pool_id": pid, "question_type": "美食"}).output  # 旧单选
        assert out["question_count"] == 1 and "红烧肉" in out["question_text"]
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_empty_means_all(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        pid, uid = _make_pool(app, [("美食", "a", True), ("旅游", "b", True), (None, "c", True), ("美食", "停用", False)])
        out = _run(app, uid, {"pool_id": pid}).output  # 无 types 无 record_ids
        assert out["question_count"] == 3  # 全部 active
        assert "停用" not in out["question_text"]
    finally:
        app.cleanup()
```

- [ ] **Step 2: 运行确认失败**

Run: `docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_question_source_multiselect.py -q'`
Expected: 多数 FAIL（现节点不认 question_types/question_record_ids，只按单 question_type）。`test_legacy_question_type` 可能已 PASS。

- [ ] **Step 3: 重写节点**

整体替换 `server/app/modules/pipelines/nodes/question_source.py`：
```python
from server.app.modules.pipelines.nodes.base import NodeResult, NodeRunContext, register
from server.app.shared.errors import ValidationError


def run_question_source(ctx: NodeRunContext) -> NodeResult:
    from sqlalchemy import or_

    from server.app.modules.ai_generation.models import QuestionItem, QuestionPool
    from server.app.modules.system.models import User

    cfg = ctx.config or {}
    pool_id = cfg.get("pool_id")
    if not pool_id:
        raise ValidationError("question_source 节点需配置 pool_id")

    # 多选类型；向后兼容旧单选 question_type
    question_types = cfg.get("question_types")
    if question_types is None:
        legacy = cfg.get("question_type")
        question_types = [] if (legacy is None or legacy == "") else [legacy]
    question_record_ids = cfg.get("question_record_ids") or []

    db = ctx.session_factory()
    try:
        pool = db.get(QuestionPool, pool_id)
        if pool is None or getattr(pool, "is_deleted", False):
            raise ValidationError("问题池不存在")
        if pool.user_id != ctx.user_id:
            user = db.get(User, ctx.user_id)
            if user is None or user.role != "admin":
                raise ValidationError("无权访问该问题池")

        query = db.query(QuestionItem.question_text).filter(
            QuestionItem.pool_id == pool_id,
            QuestionItem.source_active.is_(True),
        )
        if question_record_ids:
            # 精选：record_id 命中且仍 active；失效/不存在的自动跳过
            query = query.filter(QuestionItem.record_id.in_(question_record_ids))
        elif question_types:
            named = [t for t in question_types if t != "__uncategorized__"]
            conds = []
            if named:
                conds.append(QuestionItem.category.in_(named))
            if "__uncategorized__" in question_types:
                conds.append(QuestionItem.category.is_(None))
            if conds:
                query = query.filter(or_(*conds))
        # else: 空 → 不过滤，取整池
        rows = query.order_by(QuestionItem.id.asc()).all()
        texts = [(r[0] or "").strip() for r in rows if (r[0] or "").strip()]
    finally:
        db.close()

    rendered = "\n".join(f"{i}. {t}" for i, t in enumerate(texts, start=1))
    return NodeResult(
        output={"question_text": rendered, "question_count": len(texts)},
        article_ids=[],
    )


register("question_source", run_question_source)
```

- [ ] **Step 4: 改 node-types config_schema**

`router.py:get_node_types()` 里 question_source 的 config_schema 改为：
```python
                "config_schema": [
                    {"key": "pool_id", "type": "question_pool", "label": "问题池"},
                    {"key": "question_types", "type": "question_types",
                     "label": "问题类型（多选，留空=全部）"},
                    {"key": "question_record_ids", "type": "question_records",
                     "label": "具体问题（可选，留空=上述类型全部）"},
                ],
```
（删除原 `{"key":"question_type","type":"question_type",...}` 项。）

- [ ] **Step 5: 运行通过 + ruff**

Run:
```
docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_question_source_multiselect.py server/tests/test_ai_generation_nodes.py -q && pip install ruff -q 2>/dev/null; ruff check server/app/modules/pipelines/nodes/question_source.py server/app/modules/pipelines/router.py server/tests/test_question_source_multiselect.py && ruff format --check server/app/modules/pipelines/nodes/question_source.py server/app/modules/pipelines/router.py server/tests/test_question_source_multiselect.py'
```
Expected: 6 新例 + `test_ai_generation_nodes.py` 既有 question_source 例（PR#30，传单 question_type 仍兼容）全 PASS + ruff clean。

- [ ] **Step 6: 提交**

```bash
git add server/app/modules/pipelines/nodes/question_source.py server/app/modules/pipelines/router.py server/tests/test_question_source_multiselect.py
git commit -m "feat(pipelines): question_source 多选类型 + 精选具体问题(record_id)，兼容旧单选"
```

---

## Task 2: 前端编辑器（两个联动多选）

**Files:**
- Modify: `web/src/features/pipelines/PipelineEditor.tsx`

- [ ] **Step 1: typesByPool 缓存改为存完整 QuestionType[]**

把 import 补上 `QuestionType`（来自 `../../types`），state 改：
```tsx
  // 每个池缓存完整问题类型(含各类问题)，供"类型多选"与"具体问题多选"联动。
  const [typesByPool, setTypesByPool] = useState<Record<number, QuestionType[]>>({});
```
（`web/src/types.ts` 已有 `QuestionType`；确认 PipelineEditor 顶部 `import type { ... } from "../../types";` 里加上 `QuestionType`。）

- [ ] **Step 2: ensureTypes 存原始结构**

```tsx
  const ensureTypes = useCallback((poolId: number) => {
    if (poolId && typesByPool[poolId] === undefined) {
      listQuestionTypes(poolId)
        .then((ts) => setTypesByPool((m) => ({ ...m, [poolId]: ts })))
        .catch(() => setTypesByPool((m) => ({ ...m, [poolId]: [] })));
    }
  }, [typesByPool]);
```

- [ ] **Step 3: 替换 `question_type` 渲染分支为 `question_types` + `question_records`**

把三元链中这一段（`: f.type === "question_type" ? (() => { ... })()`）整体替换为下面两段（注意：局部变量勿用 `selected`）：
```tsx
                    : f.type === "question_types"
                    ? (() => {
                        const poolId = Number(sel.config["pool_id"]) || 0;
                        const types = typesByPool[poolId] ?? [];
                        if (poolId) ensureTypes(poolId);
                        const picked = (sel.config[f.key] as string[] | undefined) ?? [];
                        return (
                          <select className="peMultiSelect" multiple disabled={!poolId}
                            value={picked}
                            onChange={(e) => updateNode(selected!, { config: { ...sel.config,
                              [f.key]: Array.from(e.target.selectedOptions, (o) => o.value) } })}>
                            {types.map((t) => {
                              const v = t.question_type ?? "__uncategorized__";
                              return <option key={v} value={v}>{t.question_type ?? "未分类"}（{t.count}）</option>;
                            })}
                          </select>
                        );
                      })()
                    : f.type === "question_records"
                    ? (() => {
                        const poolId = Number(sel.config["pool_id"]) || 0;
                        const types = typesByPool[poolId] ?? [];
                        if (poolId) ensureTypes(poolId);
                        const selTypes = (sel.config["question_types"] as string[] | undefined) ?? [];
                        const inScope = (t: QuestionType) =>
                          selTypes.length === 0 || selTypes.includes(t.question_type ?? "__uncategorized__");
                        const questions = types.filter(inScope).flatMap((t) => t.questions);
                        const picked = (sel.config[f.key] as string[] | undefined) ?? [];
                        return (
                          <select className="peMultiSelect" multiple disabled={!poolId}
                            value={picked}
                            onChange={(e) => updateNode(selected!, { config: { ...sel.config,
                              [f.key]: Array.from(e.target.selectedOptions, (o) => o.value) } })}>
                            {questions.map((q) => (
                              <option key={q.record_id} value={q.record_id}>{q.question_text}</option>
                            ))}
                          </select>
                        );
                      })()
```
> 说明：`question_types` value 是 `string[]`（分类名 / `__uncategorized__`）；`question_records` value 是 `string[]`（record_id），选项范围随 `question_types` 联动（留空＝全部类型的问题）。`updateNode(selected!, ...)` 里的 `selected` 是组件已有的节点下标 state，照用；局部用了 `picked`/`types`/`selTypes` 避免重名。

- [ ] **Step 4: typecheck + build**

Run（host）：`pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 通过。若报 `QuestionType` 未导入 → 在顶部 type import 补上；若报旧 `question_type` 残留引用 → 确认该分支已整体删除。

- [ ] **Step 5: 提交**

```bash
git add web/src/features/pipelines/PipelineEditor.tsx
git commit -m "feat(web): 问题源节点 类型多选 + 具体问题多选(联动)，替换旧单选"
```

---

## Task 3: 回归 + 验证

**Files:** 无（仅验证）

- [ ] **Step 1: 后端回归**

Run:
```
docker exec geo-collab-app-1 sh -lc 'cd /app && GEO_TEST_DATABASE_URL="mysql+pymysql://geo_user:geo_pass_dev@mysql:3306/geo_test" python -m pytest server/tests/test_question_source_multiselect.py server/tests/test_ai_generation_nodes.py -q'
```
Expected: 全 PASS（新例 + PR#30 既有 question_source/ai_compose/to_review/端到端 例）。

- [ ] **Step 2: 前端 typecheck + build**

Run（host）：`pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
Expected: 通过。

- [ ] **Step 3: （可选）live 烟雾**

若 5173/8000 在跑：`/api/pipelines/node-types` 的 question_source 应含 `question_types` + `question_record_ids` 两字段；节点面板选问题池后两个多选可联动。

> 本任务无代码改动、无需提交。

---

## Self-Review 结果

- **Spec 覆盖**：§3 节点语义（record_ids 优先/类型 live/未分类/旧兼容/空=全部/失效宽容）→ Task1 Step3 + 6 测试；§4 node-types → Task1 Step4；§4 前端两联动多选 + typesByPool 扩展 + 移除旧单选 → Task2；§5 向后兼容 → Task1 Step3 映射 + Task1 Step5 跑 PR#30 回归；§6 测试 → Task1 测试 + Task3；§7 验收 1-5 → 各 Task。
- **占位符**：无 TBD；每步完整代码；唯一"先确认"＝前端 import/旧分支删除（Task2 Step4 给了排错指引）。
- **类型一致**：节点读 `question_types: list[str]` / `question_record_ids: list[str]`（含 `__uncategorized__` 哨兵），与 node-types 字段 key、前端写入键一致；`question_records` 用 record_id（string），与节点 `record_id.in_(...)` 一致；`typesByPool: QuestionType[]` 与 `listQuestionTypes` 返回一致，且两个渲染分支都从它派生。
- **变量名安全**：新分支局部用 `picked/types/selTypes/poolId`，未覆盖组件的 `selected`/`sel`。
- **不牵连其它节点**：仅 question_source 用过 `question_type` 字段类型，移除其渲染分支安全；其余节点字段类型不变。
- **无 DB 迁移**：config 为 JSON，旧 `question_type` 运行时映射。
