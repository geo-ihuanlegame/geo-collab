# 用户测试反馈修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复四个用户测试反馈问题：AI 排版提速、配图逻辑重写（随机取图）、跨工作台数据同步（isActive 刷新）、ZIP 导入账号 cookie 状态校验。

**Architecture:** 后端改动集中在 `ai_format.py`（去重节点、重写选图逻辑）和 `auth.py`（新增 cookie 校验函数）；前端改动在 `App.tsx` 传递 `isActive` prop，各工作台在激活时刷新自身数据，TasksWorkspace SSE 结束后补一次兜底拉取。

**Tech Stack:** Python/FastAPI (backend), React 19 + TypeScript (frontend), pytest (backend tests), LiteLLM + Jinja2 (AI prompt)

---

## 文件变更清单

| 文件 | 类型 |
|------|------|
| `server/app/modules/articles/ai_format.py` | 修改 |
| `server/app/modules/articles/prompts/ai_format_with_images.j2` | 修改 |
| `server/app/modules/accounts/auth.py` | 修改 |
| `server/tests/test_accounts_import_export.py` | 修改（更新断言 + 新增单元测试） |
| `web/src/App.tsx` | 修改 |
| `web/src/features/content/ContentWorkspace.tsx` | 修改 |
| `web/src/features/tasks/TasksWorkspace.tsx` | 修改 |
| `web/src/features/accounts/AccountsWorkspace.tsx` | 修改 |

---

## Task 1：AI 排版提速 — 去掉重复节点列表

**根因：** `run_ai_format` 把节点列表写进了 system prompt（通过 Jinja 模板），又在 user message 里重复了一遍，导致长文章 token 翻倍。

**Files:**
- Modify: `server/app/modules/articles/ai_format.py`

- [ ] **Step 1: 定位 `run_ai_format` 中的 `listing` 变量**

打开 `server/app/modules/articles/ai_format.py`，找到 `run_ai_format` 函数（约 573 行开始）。找到以下两个代码块：

```python
listing = "\n".join(
    f"{i} {_node_label(node)}: {_node_text(node)}" for i, node in text_nodes
)
```

和：

```python
messages=[
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": listing},
],
```

- [ ] **Step 2: 删除 `listing` 变量，更新 user message**

将上述两处改为：

```python
# listing 变量整行删除（不再需要）

# messages 改为：
messages=[
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": "请按上述要求完成分析，仅返回 JSON。"},
],
```

- [ ] **Step 3: 运行现有 AI format 测试确认不破坏**

```bash
conda activate geo_xzpt
pytest server/tests/test_ai_format.py -v
```

预期：所有测试 PASS（该改动不改变逻辑，仅减少重复 token）。

- [ ] **Step 4: Commit**

```bash
git add server/app/modules/articles/ai_format.py
git commit -m "perf: remove duplicate node listing from ai_format user message

Nodes were sent twice: embedded in system prompt via Jinja template
and repeated verbatim in the user message. Remove the redundant listing
to halve token usage for long articles."
```

---

## Task 2：配图 Prompt — 去掉 hint，改为栏目匹配

**根因：** Jinja 模板和内置 prompt 都要求 AI 输出 `hint` 关键词。新需求是：段落能对应某个栏目就随机取图，不需要 hint。另外，`_build_system_prompt_with_images`、第一个 `_fallback_prompt`、第一个 `_load_ai_format_prompt` 三个函数已被文件下方的重名函数覆盖，属于死代码，一并清除。

**Files:**
- Modify: `server/app/modules/articles/prompts/ai_format_with_images.j2`
- Modify: `server/app/modules/articles/ai_format.py`

- [ ] **Step 1: 更新 j2 模板**

将 `server/app/modules/articles/prompts/ai_format_with_images.j2` 全文替换为：

```jinja2
你是文章正文排版助手，只处理正文顶层节点，不处理文章主标题。

输入格式：每行一个顶层节点，格式为：
  <原始索引> [段落] 文本内容
  <原始索引> [小标题] 文本内容

以下是文章所有顶层节点：
{% for node in text_nodes -%}
  {{ node.index }} {{ node.label }}: {{ node.text }}
{% endfor %}

{% if available_categories %}
以下是本文允许使用的游戏图片栏目，你只能从这些 category_id 中选择，不要编造：
{% for category in available_categories -%}
  - category_id={{ category.id }}，栏目名={{ category.name }}{% if category.description %}，描述={{ category.description }}{% endif %}
{% endfor %}
{% else %}
本文没有可用游戏图片栏目，image_positions 返回空数组。
{% endif %}

你需要完成两项判断：

【小标题判断】
找出应设为正文小标题（H1）的节点索引。
- 小标题特征：短句、章节引导语、概括性短语，通常不超过 20 字
- 不是小标题：完整叙述句、解释说明、数据陈述
- 宁少勿多，不确定就不选
- 不生成新标题，不改写任何文字

【配图位置判断】
对每个段落节点，判断其内容是否明确属于某个可用游戏栏目，若是则记录该位置和对应 category_id。
- 只在段落节点后插图，不在小标题后插图
- 相邻配图间距不少于 {{ min_spacing }} 个节点
- 全文配图不超过 {{ max_images }} 张
- 只有能明确判断段落所属游戏栏目时才插图，不确定就不插
- category_id 必须来自上面的可用栏目列表，不要编造
- 若无明显适合位置，image_positions 返回空数组

返回：仅返回一行 JSON，不添加任何解释：
{"heading_indices":[2,7],"image_positions":[{"index":4,"category_id":12}]}
```

- [ ] **Step 2: 清理 `ai_format.py` 中的死代码**

在 `server/app/modules/articles/ai_format.py` 中，删除以下三个已被后续同名函数覆盖的函数（它们在文件前半段，约 35–129 行范围内）：

1. `_build_system_prompt_with_images`（整个函数体，约 58–101 行）
2. 第一个 `_fallback_prompt` 定义（约 104–107 行，只有 4 行）
3. 第一个 `_load_ai_format_prompt` 定义（约 110–129 行）

删除后确认文件中仍保留：
- `_image_prompt_params`（约 35 行，保留）
- `_SYSTEM_PROMPT_HEADINGS_ONLY`（顶部常量，保留）
- 第二个 `_fallback_prompt`（约 263–278 行，保留）
- 第二个 `_load_ai_format_prompt`（约 281–305 行，保留）

- [ ] **Step 3: 运行 AI format 测试**

```bash
pytest server/tests/test_ai_format.py -v
```

预期：PASS。

- [ ] **Step 4: Commit**

```bash
git add server/app/modules/articles/prompts/ai_format_with_images.j2
git add server/app/modules/articles/ai_format.py
git commit -m "feat: simplify ai_format prompt to category-based image insertion

Remove hint field from prompt and response format. AI now only needs
to identify which paragraph belongs to which category_id. Dead code
(_build_system_prompt_with_images and first _fallback_prompt/_load_ai_format_prompt
definitions) also removed."
```

---

## Task 3：配图后端 — 简化 `_maybe_insert_images` 改用随机取图

**根因：** 当前逻辑：`category_id` 存在时仍通过 `select_images_by_hints` 做 hint 匹配，`hint=None` 时返回 `None` → 静默不插图。新逻辑：`category_id` 存在 → 直接随机取图（`pick_image_id`），不再依赖 hint。

**Files:**
- Modify: `server/app/modules/articles/ai_format.py`

- [ ] **Step 1: 更新顶部 selector 导入**

找到文件顶部的 import（约第 15–16 行）：

```python
from server.app.modules.image_library.selector import fetch_image_by_id, select_images_by_hints
```

替换为：

```python
from server.app.modules.image_library.selector import fetch_image_by_id, pick_image_id, ImageQuery
```

- [ ] **Step 2: 删除 `_legacy_maybe_insert_images_old_unused` 函数**

找到 `_legacy_maybe_insert_images_old_unused`（约 432–485 行，函数名里已标注 "old_unused"），整个函数删除。

- [ ] **Step 3: 重写 `_maybe_insert_images` 函数**

找到 `_maybe_insert_images`（约 488–551 行），将整个函数替换为：

```python
def _maybe_insert_images(content_json: dict, parsed: dict, article: Any, db: Any) -> tuple[dict, int]:
    """按 AI 返回的 category_id 随机取图插入正文。

    规则：
    - 每个 image_position 必须携带有效的 category_id（属于文章的可用栏目）
    - 找到 category_id 后随机取一张图（不做 hint/语义匹配）
    - 没有匹配栏目的位置直接跳过
    """
    if has_images_in_content(content_json):
        return content_json, 0

    category_ids: list[int] = [cat["id"] for cat in _available_categories_for_article(article, db)]
    if not category_ids:
        return content_json, 0

    image_positions_raw = parsed.get("image_positions", [])
    if not isinstance(image_positions_raw, list) or not image_positions_raw:
        return content_json, 0

    positions: list[int] = []
    requested_category_ids: list[int | None] = []
    for item in image_positions_raw:
        if isinstance(item, dict):
            idx = item.get("index")
            category_id = item.get("category_id")
            if isinstance(idx, int):
                positions.append(idx)
                requested_category_ids.append(category_id if isinstance(category_id, int) else None)
        elif isinstance(item, int):
            positions.append(item)
            requested_category_ids.append(None)

    if not positions:
        return content_json, 0

    valid_category_ids = set(category_ids)
    matched_refs = []
    matched_positions = []
    used_ids: list[int] = []

    for pos, requested_category_id in zip(positions, requested_category_ids):
        if requested_category_id is None or requested_category_id not in valid_category_ids:
            continue
        image_id = pick_image_id(
            ImageQuery(category_ids=[requested_category_id], excluded_ids=used_ids), db
        )
        if image_id is None:
            continue
        ref = fetch_image_by_id(image_id, db)
        if ref is not None:
            used_ids.append(image_id)
            matched_refs.append(ref)
            matched_positions.append(pos)

    if not matched_refs:
        return content_json, 0

    return insert_images_at_positions(content_json, matched_refs, matched_positions), len(matched_refs)
```

- [ ] **Step 4: 运行测试**

```bash
pytest server/tests/test_ai_format.py server/tests/test_image_library_inserter.py -v
```

预期：PASS。

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/articles/ai_format.py
git commit -m "fix: rewrite _maybe_insert_images to use random category-based selection

Previous implementation called select_images_by_hints() even when
category_id was provided. With hint=None the function silently returned
None, causing images to never be inserted despite a valid category_id.

New implementation: when category_id is present and valid, pick a
random image from that category via pick_image_id(). No hint matching.
Also removed _legacy_maybe_insert_images_old_unused dead function."
```

---

## Task 4：ZIP 导入 — cookie 有效性评估

**根因：** `import_accounts_auth_package` 直接信任 manifest 里的 `status`，不验证 storage_state.json 里的 cookies 是否过期。

**Files:**
- Modify: `server/app/modules/accounts/auth.py`
- Modify: `server/tests/test_accounts_import_export.py`

- [ ] **Step 1: 写失败测试（新增单元测试，不需要 DB）**

在 `server/tests/test_accounts_import_export.py` 文件末尾追加：

```python
# ── 单元测试：_assess_imported_status ──────────────────────────────────────

def test_assess_imported_status_empty_cookies(tmp_path):
    import json
    from server.app.modules.accounts.auth import _assess_imported_status

    state_file = tmp_path / "storage_state.json"
    state_file.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    assert _assess_imported_status(state_file) == "expired"


def test_assess_imported_status_all_expired(tmp_path):
    import json, time
    from server.app.modules.accounts.auth import _assess_imported_status

    state_file = tmp_path / "storage_state.json"
    past = time.time() - 3600  # 1 小时前
    state_file.write_text(
        json.dumps({"cookies": [{"name": "sid", "expires": past}], "origins": []}),
        encoding="utf-8",
    )
    assert _assess_imported_status(state_file) == "expired"


def test_assess_imported_status_session_cookie(tmp_path):
    import json
    from server.app.modules.accounts.auth import _assess_imported_status

    state_file = tmp_path / "storage_state.json"
    # expires=-1 表示 session cookie（无明确过期时间）
    state_file.write_text(
        json.dumps({"cookies": [{"name": "sessionid", "expires": -1}], "origins": []}),
        encoding="utf-8",
    )
    assert _assess_imported_status(state_file) == "valid"


def test_assess_imported_status_future_cookie(tmp_path):
    import json, time
    from server.app.modules.accounts.auth import _assess_imported_status

    state_file = tmp_path / "storage_state.json"
    future = time.time() + 86400  # 明天到期
    state_file.write_text(
        json.dumps({"cookies": [{"name": "auth", "expires": future}], "origins": []}),
        encoding="utf-8",
    )
    assert _assess_imported_status(state_file) == "valid"


def test_assess_imported_status_invalid_json(tmp_path):
    from server.app.modules.accounts.auth import _assess_imported_status

    state_file = tmp_path / "storage_state.json"
    state_file.write_text("this is not json", encoding="utf-8")
    assert _assess_imported_status(state_file) == "unknown"
```

- [ ] **Step 2: 运行新测试确认失败（函数还不存在）**

```bash
pytest server/tests/test_accounts_import_export.py::test_assess_imported_status_empty_cookies -v
```

预期：`ImportError: cannot import name '_assess_imported_status'`

- [ ] **Step 3: 在 `auth.py` 中新增 `_assess_imported_status` 函数**

在 `server/app/modules/accounts/auth.py` 中，找到 `import_accounts_auth_package` 函数定义（约 834 行），在它**上方**插入：

```python
def _assess_imported_status(state_path: Path) -> str:
    """解析 storage_state.json 评估 cookie 有效性。

    返回：
      "valid"   — cookies 非空，且至少有一个 session cookie（expires=-1）
                  或尚未过期的 cookie
      "expired" — cookies 数组为空，或全部 cookie 时间戳已过期
      "unknown" — 文件读取或 JSON 解析失败
    """
    import time as _time

    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return "unknown"

    cookies = data.get("cookies") or []
    if not cookies:
        return "expired"

    now = _time.time()
    for cookie in cookies:
        expires = cookie.get("expires", -1)
        if expires == -1 or expires > now:
            return "valid"

    return "expired"
```

- [ ] **Step 4: 在 `import_accounts_auth_package` 中使用新函数**

在 `import_accounts_auth_package` 函数内，找到这段代码（紧接在 `dest.write_bytes(...)` 之后，约 890–894 行）：

```python
            _valid_statuses = {"valid", "expired", "unknown"}
            imported_status = entry.get("status", "unknown")
            if imported_status not in _valid_statuses:
                imported_status = "unknown"
```

替换为：

```python
            imported_status = _assess_imported_status(dest)
```

- [ ] **Step 5: 更新 `test_export_import_round_trip` 测试断言**

在 `server/tests/test_accounts_import_export.py` 的 `test_export_import_round_trip` 函数中，找到：

```python
        assert imported["status"] == "valid"
```

改为：

```python
        # 导出的 storage_state.json 里 cookies 为空（测试辅助函数写入空 cookies）
        # 新逻辑：空 cookies → "expired"
        assert imported["status"] == "expired"
```

- [ ] **Step 6: 运行所有 import/export 测试**

```bash
pytest server/tests/test_accounts_import_export.py -v
```

预期：全部 PASS（包括新增的 5 个单元测试）。

- [ ] **Step 7: Commit**

```bash
git add server/app/modules/accounts/auth.py server/tests/test_accounts_import_export.py
git commit -m "fix: validate cookie expiry on ZIP account import

Previously, import_accounts_auth_package trusted the 'status' field
from the manifest (could be 'valid') without checking if the actual
cookies in storage_state.json were still alive.

New _assess_imported_status() parses the cookies array:
- empty cookies → 'expired'
- all timestamps past → 'expired'
- any session cookie (expires=-1) or future timestamp → 'valid'
- parse failure → 'unknown'

Updated test_export_import_round_trip to expect 'expired' (test helper
writes empty cookies file)."
```

---

## Task 5：前端 — `App.tsx` 传递 `isActive` prop

**根因：** 各工作台通过 `display:none` 隐藏而非卸载，没有"重新激活"事件，导致跨工作台数据永不刷新。解决方案：父组件把当前活跃状态通过 prop 告知各工作台。

**Files:**
- Modify: `web/src/App.tsx`

- [ ] **Step 1: 给三个工作台加 `isActive` prop**

在 `web/src/App.tsx` 中，找到以下三处 workspace 渲染，分别加上 `isActive` prop：

```tsx
// ContentWorkspace（约第 108 行，已有 dirtyCheckRef）：
<ContentWorkspace dirtyCheckRef={contentDirtyRef} isActive={activeNav === "content"} />

// AccountsWorkspace（约第 129 行）：
<AccountsWorkspace isActive={activeNav === "media"} />

// TasksWorkspace（约第 135 行）：
<TasksWorkspace isActive={activeNav === "tasks"} />
```

- [ ] **Step 2: 前端类型检查**

```bash
pnpm --filter @geo/web typecheck
```

预期：此步骤会出现 TS 报错（`isActive` 还未在各 workspace 的 Props 定义里）。这是预期的，继续 Task 6。

---

## Task 6：前端 — 各工作台实现 `isActive` 刷新逻辑

**规则：** 初始 mount 时跳过（避免双重加载），`isActive` 从 `false` 变为 `true` 时刷新本工作台的核心数据。

**Files:**
- Modify: `web/src/features/content/ContentWorkspace.tsx`
- Modify: `web/src/features/tasks/TasksWorkspace.tsx`
- Modify: `web/src/features/accounts/AccountsWorkspace.tsx`

- [ ] **Step 1: ContentWorkspace — 添加 `isActive` prop 及刷新逻辑**

在 `web/src/features/content/ContentWorkspace.tsx` 中：

**1a. 更新 Props 接口**（找到约第 314 行的 `interface Props`）：

```tsx
interface Props {
  dirtyCheckRef?: MutableRefObject<() => boolean>;
  isActive?: boolean;
}
```

**1b. 更新函数签名**（约第 318 行）：

```tsx
export function ContentWorkspace({ dirtyCheckRef, isActive }: Props = {}) {
```

**1c. 在 `useRef` 声明区域末尾（约 350 行附近）新增 mount flag ref**：

```tsx
const isInitialMountRef = useRef(true);
```

**1d. 在现有初始加载 `useEffect`（约 511 行，`[]` 依赖项的那个）之后**，紧接新增：

```tsx
useEffect(() => {
  if (isInitialMountRef.current) {
    isInitialMountRef.current = false;
    return;
  }
  if (!isActive) return;
  void refreshArticles();
  void refreshGroups();
}, [isActive]);
```

- [ ] **Step 2: TasksWorkspace — 添加 `isActive` prop 及刷新逻辑**

在 `web/src/features/tasks/TasksWorkspace.tsx` 中：

**2a. 在文件顶部 `useRef` 相关导入中确认 `useRef` 已导入**（第 1 行已有）。

**2b. 在组件 state 声明区域末尾新增**：

```tsx
const isInitialMountRef = useRef(true);
```

**2c. 在现有 `useEffect(() => { void loadInitial(); }, []);`（约第 101 行）之后**，新增：

```tsx
useEffect(() => {
  if (isInitialMountRef.current) {
    isInitialMountRef.current = false;
    return;
  }
  if (!isActive) return;
  void loadInitial();
}, [isActive]);
```

**2d. 在函数签名**（约第 52 行 `export function TasksWorkspace()`）**添加 props**：

```tsx
export function TasksWorkspace({ isActive }: { isActive?: boolean } = {}) {
```

- [ ] **Step 3: AccountsWorkspace — 添加 `isActive` prop 及刷新逻辑**

在 `web/src/features/accounts/AccountsWorkspace.tsx` 中：

**3a. 在文件顶部 React import 中补上 `useRef`**：

```tsx
// 修改前：
import { useEffect, useState } from "react";
// 修改后：
import { useEffect, useRef, useState } from "react";
```

**3b. 在组件 state 声明区域末尾新增**：

```tsx
const isInitialMountRef = useRef(true);
```

**3b. 在现有初始 `useEffect`（有 `Promise.all([listPlatforms(), listAccounts()])` 的那个）之后**，新增：

```tsx
useEffect(() => {
  if (isInitialMountRef.current) {
    isInitialMountRef.current = false;
    return;
  }
  if (!isActive) return;
  void (async () => {
    const [platformData, accountData] = await Promise.all([listPlatforms(), listAccounts()]);
    setPlatforms(platformData);
    setAccounts(accountData);
  })();
}, [isActive]);
```

**3c. 在函数签名处添加 props**（找到 `export function AccountsWorkspace()`）：

```tsx
export function AccountsWorkspace({ isActive }: { isActive?: boolean } = {}) {
```

- [ ] **Step 4: 类型检查通过**

```bash
pnpm --filter @geo/web typecheck
```

预期：PASS（0 errors）。

- [ ] **Step 5: Commit**

```bash
git add web/src/App.tsx web/src/features/content/ContentWorkspace.tsx web/src/features/tasks/TasksWorkspace.tsx web/src/features/accounts/AccountsWorkspace.tsx
git commit -m "feat: refresh workspace data on tab activation

Workspaces are hidden via display:none (never unmounted), so they
never reload stale data. Add isActive prop to Content/Tasks/Accounts
workspaces; each re-fetches its core data when isActive transitions
from false to true (skipping initial mount to avoid double-loading).

This means: create article in Content → switch to Tasks → article
immediately appears in task creation dropdown."
```

---

## Task 7：SSE 结束兜底拉取 + 账号导入提示优化

**Files:**
- Modify: `web/src/features/tasks/TasksWorkspace.tsx`
- Modify: `web/src/features/accounts/AccountsWorkspace.tsx`

- [ ] **Step 1: TasksWorkspace — SSE `done` 事件后补一次兜底拉取**

在 `web/src/features/tasks/TasksWorkspace.tsx` 中，找到 SSE `done` 事件监听器（约第 152 行）：

```tsx
es.addEventListener("done", () => {
  es.close();
  setAutoRefreshTaskIds((prev) => {
    const next = new Set(prev);
    next.delete(taskId);
    return next;
  });
});
```

替换为：

```tsx
es.addEventListener("done", () => {
  es.close();
  // SSE 关闭后 500ms 补一次拉取，确保最终日志和状态已写入
  setTimeout(() => {
    void refreshDetail(taskId).catch(() => {});
  }, 500);
  setAutoRefreshTaskIds((prev) => {
    const next = new Set(prev);
    next.delete(taskId);
    return next;
  });
});
```

- [ ] **Step 2: AccountsWorkspace — 优化导入结果提示**

在 `web/src/features/accounts/AccountsWorkspace.tsx` 中，找到调用 `importAccountPackage` 的处理函数（搜索 `importAccountPackage`）。

找到成功后的 toast 调用，更新为显示详细结果并提醒用户校验。将成功 toast 改为：

```tsx
const importedNames = result.imported;
const skippedNames = result.skipped;
const importedCount = importedNames.length;
const skippedCount = skippedNames.length;

if (importedCount > 0) {
  const skippedHint = skippedCount > 0 ? `，跳过 ${skippedCount} 个` : "";
  toast(
    `已导入 ${importedCount} 个账号${skippedHint}。Cookie 有效性取决于平台 session，请点击「校验」确认后再发布。`,
    "success",
  );
} else {
  toast(
    skippedCount > 0 ? `未导入任何账号，跳过 ${skippedCount} 个（已存在或格式无效）` : "ZIP 中未找到可导入的账号",
    "error",
  );
}
```

注意：需要在函数内将 `await importAccountPackage(file)` 的返回值赋给变量 `result`，如果原来没有的话。

- [ ] **Step 3: 类型检查**

```bash
pnpm --filter @geo/web typecheck
```

预期：PASS。

- [ ] **Step 4: Commit**

```bash
git add web/src/features/tasks/TasksWorkspace.tsx web/src/features/accounts/AccountsWorkspace.tsx
git commit -m "fix: add SSE done fallback fetch and improve ZIP import toast

TasksWorkspace: after SSE 'done' event, schedule a 500ms delayed
refreshDetail() to capture any final log entries written after SSE
stream closes.

AccountsWorkspace: show import count, skip count, and reminder to
verify account sessions after ZIP import."
```

---

## 验收检查

完成所有 Task 后，手动验证：

1. **AI 排版速度**：对一篇 20 段以上的文章触发 AI 格式，观察等待时间是否明显缩短。
2. **配图插入**：创建含游戏名称段落的文章，确保配图栏目存在时随机插入图片（不再需要 hint 匹配）。
3. **跨工作台同步**：在内容工作台新建/保存文章 → 切换到分发引擎 → 立即在创建任务的文章下拉中能看到该文章。
4. **ZIP 导入状态**：导入含空 cookies 的授权包 → 账号状态应为 `expired`；导入含有效 session cookie 的包 → 状态为 `valid`。
5. **任务完成日志**：执行任务直至完成，日志在任务结束后几秒内自动出现，无需 F5。
