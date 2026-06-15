# 发布前随机延迟（anti-ban pre-publish delay）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每条发布在调用驱动发文前插入一个随机延迟（默认 10s–2min，可配置、默认开启），把同账号发文节奏拉散以防风控，并自动加宽执行超时预算以免延迟撞上现有 300s 硬墙。

**Architecture:** 在浏览器/API 两类驱动的唯一共同入口 `executor._publish_record()`（持有全局并发槽之后）插入一次延迟；新增一个按最大延迟自动加宽的执行预算函数，替换 `executor.py` 里 4 处用到 `publish_record_timeout_seconds` 作时间预算的地方。纯配置 + 代码，无 DB 迁移。

**Tech Stack:** Python 3 / FastAPI / pydantic-settings / pytest。先 `conda activate geo_xzpt`。本计划的单测**不需要 DB**（不打 `@pytest.mark.mysql`），裸跑 pytest 即可。

设计稿：`docs/superpowers/specs/2026-06-15-publish-pre-delay-design.md`

---

## File Structure

- **Modify** `server/app/core/config.py`：`Settings` 新增 3 个字段（`publish_pre_delay_enabled` / `publish_pre_delay_min_seconds` / `publish_pre_delay_max_seconds`），并在模块 docstring 列出对应环境变量。
- **Modify** `server/app/modules/tasks/executor.py`：
  - 顶部新增 `import random`。
  - 新增模块级函数 `_record_execution_budget()` 与 `_maybe_pre_publish_delay()`。
  - 替换 4 处执行预算用法（行 178 / 288-289 / 381 / 489）与 1 处超时错误文案（行 294-299）。
  - 在 `_publish_record()` 的诊断作用域内、构建 runner 之前插入延迟调用。
- **Create** `server/tests/test_publish_pre_delay.py`：配置默认/覆盖、预算加宽、延迟函数四种分支的单测（注入假 `sleep`/`rng`，零真实等待）。

任务顺序刻意为：配置 → 预算加宽 → 延迟本体。先加宽预算再开延迟，避免中间提交出现「延迟已开但超时未加宽」的可撞墙状态。

---

### Task 1: 新增配置字段

**Files:**
- Modify: `server/app/core/config.py`（docstring 与 `Settings`，约行 1-18 与 53）
- Test: `server/tests/test_publish_pre_delay.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `server/tests/test_publish_pre_delay.py`：

```python
from server.app.core.config import Settings


def test_pre_delay_defaults(monkeypatch):
    for var in (
        "GEO_PUBLISH_PRE_DELAY_ENABLED",
        "GEO_PUBLISH_PRE_DELAY_MIN_SECONDS",
        "GEO_PUBLISH_PRE_DELAY_MAX_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert s.publish_pre_delay_enabled is True
    assert s.publish_pre_delay_min_seconds == 10.0
    assert s.publish_pre_delay_max_seconds == 120.0


def test_pre_delay_env_override(monkeypatch):
    monkeypatch.setenv("GEO_PUBLISH_PRE_DELAY_ENABLED", "false")
    monkeypatch.setenv("GEO_PUBLISH_PRE_DELAY_MIN_SECONDS", "5")
    monkeypatch.setenv("GEO_PUBLISH_PRE_DELAY_MAX_SECONDS", "30")
    s = Settings()
    assert s.publish_pre_delay_enabled is False
    assert s.publish_pre_delay_min_seconds == 5.0
    assert s.publish_pre_delay_max_seconds == 30.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest server/tests/test_publish_pre_delay.py -v`
Expected: FAIL —`AttributeError: 'Settings' object has no attribute 'publish_pre_delay_enabled'`

- [ ] **Step 3: 加字段**

`server/app/core/config.py`，在 `publish_record_timeout_seconds: int = 300`（行 53）下方新增：

```python
    publish_record_timeout_seconds: int = 300
    # 发布前随机延迟（错峰防封）。enabled 默认开启；每条发布在调用驱动发文前
    # sleep random.uniform(min, max) 秒。stop_before_publish 的人工确认流程不延迟。
    publish_pre_delay_enabled: bool = True  # GEO_PUBLISH_PRE_DELAY_ENABLED
    publish_pre_delay_min_seconds: float = 10.0  # GEO_PUBLISH_PRE_DELAY_MIN_SECONDS
    publish_pre_delay_max_seconds: float = 120.0  # GEO_PUBLISH_PRE_DELAY_MAX_SECONDS
```

并在模块 docstring「本地开发关键配置」一段（行 9-10 附近）补一行说明：

```python
  GEO_PUBLISH_MAX_CONCURRENT_RECORDS  并发发布记录数（上限 5）
  GEO_PUBLISH_PRE_DELAY_ENABLED       发布前随机延迟开关（默认 true，范围 GEO_PUBLISH_PRE_DELAY_MIN/MAX_SECONDS，默认 10/120）
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest server/tests/test_publish_pre_delay.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add server/app/core/config.py server/tests/test_publish_pre_delay.py
git commit -m "feat(publish): 新增发布前随机延迟配置项（默认开启 10-120s）"
```

---

### Task 2: 执行预算自动加宽

**Files:**
- Modify: `server/app/modules/tasks/executor.py`（新增 `_record_execution_budget()`；替换行 178 / 288-289 / 294-299 / 381 / 489）
- Test: `server/tests/test_publish_pre_delay.py`（追加）

- [ ] **Step 1: 写失败测试**

在 `server/tests/test_publish_pre_delay.py` 顶部补充 import 与 helper，并追加两个测试：

```python
from types import SimpleNamespace

from server.app.modules.tasks import executor


def _fake_settings(**kw):
    base = dict(
        publish_record_timeout_seconds=300,
        publish_pre_delay_enabled=True,
        publish_pre_delay_min_seconds=10.0,
        publish_pre_delay_max_seconds=120.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_budget_extended_when_enabled(monkeypatch):
    monkeypatch.setattr(executor, "get_settings", lambda: _fake_settings(publish_pre_delay_enabled=True))
    assert executor._record_execution_budget() == 420.0


def test_budget_base_when_disabled(monkeypatch):
    monkeypatch.setattr(executor, "get_settings", lambda: _fake_settings(publish_pre_delay_enabled=False))
    assert executor._record_execution_budget() == 300
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest server/tests/test_publish_pre_delay.py -v -k budget`
Expected: FAIL —`AttributeError: module 'server.app.modules.tasks.executor' has no attribute '_record_execution_budget'`

- [ ] **Step 3: 加预算函数**

`server/app/modules/tasks/executor.py`，在 `_publish_record` 定义之前新增模块级函数：

```python
def _record_execution_budget() -> float:
    """每条记录的执行预算（秒）。开启发布前延迟时按最大延迟加宽，
    避免延迟把执行时间撞上 publish_record_timeout_seconds 硬墙。"""
    s = get_settings()
    extra = s.publish_pre_delay_max_seconds if s.publish_pre_delay_enabled else 0.0
    return s.publish_record_timeout_seconds + extra
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest server/tests/test_publish_pre_delay.py -v -k budget`
Expected: PASS（2 passed）

- [ ] **Step 5: 替换 4 处预算用法 + 错误文案**

`server/app/modules/tasks/executor.py`：

行 178（`_heartbeat_running_records`）：
```python
    new_lease = now + timedelta(seconds=get_settings().publish_record_timeout_seconds + 60)
```
→
```python
    new_lease = now + timedelta(seconds=_record_execution_budget() + 60)
```

行 288-289（执行硬墙判定）：
```python
                if time.monotonic() - running_record.started_monotonic
                > get_settings().publish_record_timeout_seconds
```
→
```python
                if time.monotonic() - running_record.started_monotonic
                > _record_execution_budget()
```

行 294-299（超时失败文案，写死的 300s 改动态）：
```python
                    _mark_record_failed(
                        db,
                        task.id,
                        running_record.record_id,
                        "Timeout: record execution exceeded 300s",
                    )
```
→
```python
                    _mark_record_failed(
                        db,
                        task.id,
                        running_record.record_id,
                        f"Timeout: record execution exceeded {int(_record_execution_budget())}s",
                    )
```

行 381（profile 锁租约）：
```python
                    lease_seconds=get_settings().publish_record_timeout_seconds + 120,
```
→
```python
                    lease_seconds=_record_execution_budget() + 120,
```

行 489（`_claim_record` 记录租约）：
```python
    lease_until = now + timedelta(seconds=get_settings().publish_record_timeout_seconds + 60)
```
→
```python
    lease_until = now + timedelta(seconds=_record_execution_budget() + 60)
```

- [ ] **Step 6: 静态检查 + 测试**

Run: `ruff check server/app/modules/tasks/executor.py && mypy server/app && pytest server/tests/test_publish_pre_delay.py -v -k budget`
Expected: ruff/mypy 无新增错误；pytest PASS。

确认没有遗漏的功能性用法：
Run: `grep -n "publish_record_timeout_seconds" server/app/modules/tasks/executor.py`
Expected: 仅剩注释/docstring 行（约行 231），无 `get_settings().publish_record_timeout_seconds` 的可执行调用残留。

- [ ] **Step 7: 提交**

```bash
git add server/app/modules/tasks/executor.py server/tests/test_publish_pre_delay.py
git commit -m "feat(publish): 执行超时预算按最大延迟自动加宽"
```

---

### Task 3: 延迟本体 + 接入发布入口

**Files:**
- Modify: `server/app/modules/tasks/executor.py`（顶部 `import random`；新增 `_maybe_pre_publish_delay()`；在 `_publish_record` 内插入调用）
- Test: `server/tests/test_publish_pre_delay.py`（追加）

- [ ] **Step 1: 写失败测试**

在 `server/tests/test_publish_pre_delay.py` 追加四个测试（复用 Task 2 的 `executor` import 与 `_fake_settings`）：

```python
def test_delay_called_within_range(monkeypatch):
    monkeypatch.setattr(executor, "get_settings", lambda: _fake_settings())
    calls = []
    executor._maybe_pre_publish_delay(
        SimpleNamespace(id=7),
        False,
        sleep=lambda d: calls.append(d),
        rng=lambda lo, hi: (lo + hi) / 2,
    )
    assert calls == [65.0]


def test_delay_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(executor, "get_settings", lambda: _fake_settings(publish_pre_delay_enabled=False))
    calls = []
    executor._maybe_pre_publish_delay(SimpleNamespace(id=1), False, sleep=lambda d: calls.append(d))
    assert calls == []


def test_delay_skipped_when_stop_before_publish(monkeypatch):
    monkeypatch.setattr(executor, "get_settings", lambda: _fake_settings())
    calls = []
    executor._maybe_pre_publish_delay(SimpleNamespace(id=1), True, sleep=lambda d: calls.append(d))
    assert calls == []


def test_delay_clamps_when_min_gt_max(monkeypatch):
    monkeypatch.setattr(
        executor,
        "get_settings",
        lambda: _fake_settings(publish_pre_delay_min_seconds=200.0, publish_pre_delay_max_seconds=120.0),
    )
    seen = {}

    def fake_rng(lo, hi):
        seen["lo"], seen["hi"] = lo, hi
        return lo

    executor._maybe_pre_publish_delay(SimpleNamespace(id=1), False, sleep=lambda d: None, rng=fake_rng)
    assert seen == {"lo": 200.0, "hi": 200.0}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest server/tests/test_publish_pre_delay.py -v -k delay`
Expected: FAIL —`AttributeError: module 'server.app.modules.tasks.executor' has no attribute '_maybe_pre_publish_delay'`

- [ ] **Step 3: 加 `import random`**

`server/app/modules/tasks/executor.py` 顶部 import 段（行 20-21 之间，`import os` 与 `import threading` 之间，保持字母序）：

```python
import os
import random
import threading
```

- [ ] **Step 4: 加延迟函数**

`server/app/modules/tasks/executor.py`，紧挨 `_record_execution_budget()` 之后新增：

```python
def _maybe_pre_publish_delay(
    record: PublishRecord,
    stop_before_publish: bool,
    *,
    sleep=time.sleep,
    rng=random.uniform,
) -> None:
    """发布前随机延迟（错峰防封）。stop_before_publish 的人工确认流程跳过。
    sleep / rng 作为可注入参数，便于测试零等待。"""
    if stop_before_publish:
        return
    s = get_settings()
    if not s.publish_pre_delay_enabled:
        return
    lo = max(0.0, s.publish_pre_delay_min_seconds)
    hi = max(lo, s.publish_pre_delay_max_seconds)
    if hi <= 0:
        return
    delay = rng(lo, hi)
    _logger.info("Pre-publish delay %.1fs for record %d", delay, record.id)
    sleep(delay)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest server/tests/test_publish_pre_delay.py -v -k delay`
Expected: PASS（4 passed）

- [ ] **Step 6: 接入 `_publish_record`**

`server/app/modules/tasks/executor.py`，`_publish_record` 内（约行 613-616）：

```python
        with capture_publish_diagnostics(diagnostics):
            runner = build_publish_runner_for_record(record)
            result = runner(article, account, stop_before_publish=stop_before_publish)
```
→
```python
        with capture_publish_diagnostics(diagnostics):
            _maybe_pre_publish_delay(record, stop_before_publish)
            runner = build_publish_runner_for_record(record)
            result = runner(article, account, stop_before_publish=stop_before_publish)
```

- [ ] **Step 7: 静态检查 + 全量新测试**

Run: `ruff check server/app/modules/tasks/executor.py && ruff format --check server/app/modules/tasks/executor.py server/tests/test_publish_pre_delay.py && mypy server/app && pytest server/tests/test_publish_pre_delay.py -v`
Expected: ruff/format/mypy 无新增错误；pytest 全绿（8 passed）。

- [ ] **Step 8: 提交**

```bash
git add server/app/modules/tasks/executor.py server/tests/test_publish_pre_delay.py
git commit -m "feat(publish): 每条发布前插入随机延迟（错峰防封）"
```

---

### Task 4: 收尾校验

**Files:** 无新增改动，仅校验。

- [ ] **Step 1: 全量静态检查**

Run: `ruff check server/ && ruff format --check server/ && mypy server/app`
Expected: 无新增错误（存量问题不在本次范围）。

- [ ] **Step 2: 跑本特性测试**

Run: `pytest server/tests/test_publish_pre_delay.py -v`
Expected: 8 passed。

- [ ] **Step 3: 人工确认回滚路径**

确认设 `GEO_PUBLISH_PRE_DELAY_ENABLED=false` 后：`_maybe_pre_publish_delay` 早返回不 sleep，`_record_execution_budget()` 回落 300 —— 行为与改动前一致。（已由 `test_delay_skipped_when_disabled` 与 `test_budget_base_when_disabled` 覆盖。）

---

## Self-Review

**1. Spec coverage（逐节对照设计稿）：**
- §3 配置三变量 → Task 1 ✓
- §4.1 插入点（`_publish_record` 信号量之后、构建 runner 之前，诊断作用域内）→ Task 3 Step 6 ✓
- §4.2 延迟函数（跳过 stop_before_publish / 关闭早返回 / 钳制 / 可注入 sleep&rng）→ Task 3 ✓
- §4.3 预算自动加宽（替换 288-289 墙 + 298 文案 + 178/381/489 租约）→ Task 2 ✓
- §5 边界（stop_before_publish 跳过、重试走同一入口、关闭=现状、账号锁 finally 不动）→ Task 3 + Task 4 Step 3 ✓
- §6 测试（开启/关闭/stop/钳制 + 预算开关）→ Task 1/2/3 全覆盖 ✓
- §8 不做（无账号维度错峰、无前端 UI、无迁移）→ 计划未触及 ✓

**2. Placeholder scan：** 无 TBD/TODO；每个代码步骤均给出完整代码与确切命令。✓

**3. Type/命名一致性：** `_record_execution_budget()` / `_maybe_pre_publish_delay()` / `_fake_settings()` 在各任务中拼写一致；配置字段名 `publish_pre_delay_enabled` / `_min_seconds` / `_max_seconds` 在 config 与测试中一致；`_logger`、`get_settings`、`PublishRecord` 均为 executor.py 既有符号。✓
