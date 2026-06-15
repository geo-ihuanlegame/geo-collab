# 发布前随机延迟（anti-ban pre-publish delay）— 设计

- 日期：2026-06-15
- 状态：已确认，待写实现计划
- 范围：后端发布链路（`server/app/modules/tasks/`、`server/app/core/config.py`）

## 1. 背景与目标

运营担心同一账号在短时间内发文过多被平台风控/封号。希望在**每条发布真正发文之前**插入一个随机延迟（默认 10s–2min），把发文节奏拉散。

由于同一账号的发布本就被账号锁串行化，给每条发布加随机延迟，等价于同账号多篇之间被自然错开，足以满足"错峰防封"诉求，且改动最小。

## 2. 已确认的决策

1. **作用范围**：每条发布记录在调用驱动发文前延迟一次（覆盖浏览器驱动与 API 驱动）。
2. **配置**：可配置 + 默认开启。新增 3 个 `GEO_` 环境变量。
3. **并发行为**：延迟期间继续占用全局并发名额（最简单、最稳）。副作用是 5 条都在延迟时整体批量吞吐下降——对防封而言是可接受甚至加分的；需要更快可调高 `GEO_PUBLISH_MAX_CONCURRENT_RECORDS`。
4. **超时**：自动加宽每条记录的执行预算，避免延迟撞上现有 300s 执行硬墙；运营不需要手动改超时环境变量。

## 3. 配置（`server/app/core/config.py`）

`Settings` 新增三个字段（前缀 `GEO_`）：

| 字段 | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `publish_pre_delay_enabled` | `GEO_PUBLISH_PRE_DELAY_ENABLED` | `True` | 总开关 |
| `publish_pre_delay_min_seconds` | `GEO_PUBLISH_PRE_DELAY_MIN_SECONDS` | `10` | 下界（秒） |
| `publish_pre_delay_max_seconds` | `GEO_PUBLISH_PRE_DELAY_MAX_SECONDS` | `120` | 上界（秒） |

健壮性：取值时 `lo = max(0, min_seconds)`，`hi = max(lo, max_seconds)`；若误配 `min > max` 按 `max` 钳制到 `lo`，不抛错。`get_settings()` 走 `@lru_cache`，测试改环境后需 `get_settings.cache_clear()`。

## 4. 实现

### 4.1 插入点

`server/app/modules/tasks/executor.py:_publish_record()` 是浏览器驱动与 API 驱动（公众号）**唯一的共同入口**（它调用 `build_publish_runner_for_record()` 再分叉到两类 runner）。在此处加一次即全覆盖，不改 `runner.py` / `runner_api.py`。

位置：`_global_publish_sem.acquire()` 之后（=占着并发名额延迟，符合决策 3）、`build_publish_runner_for_record()` 之前，且放在 `capture_publish_diagnostics(...)` 作用域内：

```python
_global_publish_sem.acquire()
try:
    with capture_publish_diagnostics(diagnostics):
        _maybe_pre_publish_delay(record, stop_before_publish)
        runner = build_publish_runner_for_record(record)
        result = runner(article, account, stop_before_publish=stop_before_publish)
        ...
finally:
    _global_publish_sem.release()
```

浏览器在 runner 内、延迟之后才启动，所以延迟期间不占浏览器，只持有本就在持有的账号锁 / profile 锁 / 并发槽。

### 4.2 延迟函数

```python
def _maybe_pre_publish_delay(record, stop_before_publish, *, sleep=time.sleep, rng=random.uniform):
    if stop_before_publish:          # 人工确认流程不实际发文，延迟无意义
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

同步代码（线程池里跑），用 `time.sleep`，不是 `asyncio.sleep`。`sleep` / `rng` 作为可注入参数，便于测试零等待。

### 4.3 执行预算自动加宽（关键）

`executor.py:288-289` 的执行硬墙从 future 提交（`started_monotonic`）起算，延迟也算在内。按最大延迟自动加宽：

```python
def _record_execution_budget() -> float:
    s = get_settings()
    extra = s.publish_pre_delay_max_seconds if s.publish_pre_delay_enabled else 0
    return s.publish_record_timeout_seconds + extra      # 默认 300 + 120 = 420
```

替换点：
- 执行硬墙判定（`executor.py:288-289`）改用 `_record_execution_budget()`。
- `executor.py:298` 写死的 `"... exceeded 300s"` 改为动态值。
- 记录租约（`executor.py:178` heartbeat、`executor.py:489`）与 profile 锁租约（`executor.py:381`）里用到的 base，改用 `_record_execution_budget()`，各自原有的 `+60` / `+120` 缓冲保留。

代价：真正卡死的发布被判超时的时间从 300s 变 420s（默认配置下）。可接受。

## 5. 边界与不变量

- `stop_before_publish=True`（头条预览停顿等人工确认）：跳过延迟。
- 重试记录：走同一入口，**也会**延迟（重试后不立即再次撞平台，合理）。
- 关闭开关（`enabled=False`）：`_maybe_pre_publish_delay` 直接返回，执行预算回落 300s，行为与现状完全一致。
- 账号锁释放仍在 `finally`（`executor.py`），延迟不改变其位置——不引入"锁泄漏到重启"的风险。
- 不涉及 DB 迁移，纯配置 + 代码。

## 6. 测试（`server/tests/`，后端；前端无关）

单元测试（注入假 `sleep` / `rng`，零真实等待）：
- 开启时：`sleep` 被调用一次，参数落在 `[min, max]`。
- 关闭时（`enabled=False`）：`sleep` 不被调用。
- `stop_before_publish=True`：`sleep` 不被调用。
- 误配 `min > max`：被钳制，不抛错，`sleep` 参数 = `max`。
- `_record_execution_budget()`：开启 = `300 + 120`；关闭 = `300`（用 `get_settings.cache_clear()` 切环境）。

这些测试不需要 DB（可不带 `@pytest.mark.mysql`），裸跑 `pytest` 即可覆盖核心逻辑。

## 7. 回滚

设 `GEO_PUBLISH_PRE_DELAY_ENABLED=false` 即可关闭，无需回滚代码；执行预算同步回落 300s。

## 8. 不做（YAGNI）

- 不做按账号/平台维度的"上次发布时间"错峰（已评估，当前 per-record 延迟即满足诉求）。
- 不做前端配置 UI（纯环境变量）。
- 不引入新的 DB 字段 / 迁移。
