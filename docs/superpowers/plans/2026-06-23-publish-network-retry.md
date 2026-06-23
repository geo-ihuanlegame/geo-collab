# 断网/弱网发布重试 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给发布链路加「网络感知重试 + 提交边界守卫」，在断网/弱网下激进重试提交前的幂等步骤、跨过提交点出网络问题则锁定为「结果未知·待人工」，兑现 at-most-once（绝不重发）。

**Architecture:** 三层。① 通用 `shared/resilience.py`（`RetryPolicy` + `retry_call`，平台无关、零 ORM）；② `drivers/base.py` 的 `CommitGuard` / `CommitUncertainError` 提交边界原语 + `PublishRecord.commit_attempted_at`/`failure_kind` 标记；③ 两类驱动接入（微信 API httpx、头条浏览器 Playwright），executor 分流 + service 层 recover/retry 护栏。

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy / Alembic / httpx / Playwright(sync) / pytest（MySQL only，`@pytest.mark.mysql` + `build_test_app`）。

## Global Constraints

- **设计来源**：`docs/superpowers/specs/2026-06-23-publish-network-retry-design.md`（方案 A，at-most-once）。
- **幂等取舍 at-most-once**：只对「提交前」幂等步骤自动重试；跨提交点的网络失败 → `CommitUncertainError` → 锁 `failure_kind=commit_uncertain`，**绝不自动重发**。
- **驱动不碰 ORM**：所有 DB 写经 runner 注入的回调；驱动只调不透明 callable（CLAUDE.md「PlatformDriver」约束）。
- **无依赖新增**：不引入 tenacity/backoff，退避自实现。`httpx`/`playwright` 已在 `requirements.txt`。
- **配置 `GEO_` 前缀 + `get_settings()` lru_cache**：测试改 env 后 `get_settings.cache_clear()`。
- **迁移排序（用户拍板）**：本计划的 `publish_records` 迁移 **后于** 并行的凭据加密 spec（`accounts` 列迁移）。Task 4 生成迁移时 `down_revision = 当前 alembic head`——若加密迁移已落 main，自然链在其后，避免迁移 DAG 双 head。改不同表、无数据冲突。
- **MySQL only 测试**：DB 测试需 `GEO_TEST_DATABASE_URL`（库名含 "test"）；纯函数测试无需 DB。命令前 `conda activate geo_xzpt`。
- **异常 except 顺序敏感**：`CommitUncertainError` 与 `UserInputRequired` 均为 `PublishError` 子类，捕获时必须排在通用 `PublishError` 分支**之前**。
- **`failure_kind` 取值**：v1 实际使用 `"commit_uncertain"` 与 `None`（列可容纳未来更多值，但本期 retry 护栏只判 `commit_uncertain`）。

---

## File Structure

**新增**
- `server/app/shared/resilience.py` — `RetryPolicy` / `retry_call` / `default_is_transient`（纯函数）
- `server/tests/test_resilience.py` — resilience 纯单测（无 DB）
- `server/tests/test_commit_guard.py` — CommitGuard 纯单测（无 DB）
- `server/alembic/versions/<rev>_publish_record_commit_markers.py` — 加两列迁移

**改动**
- `server/app/core/config.py` — 5 个 `publish_retry_*` 设置 + `get_publish_retry_policy()`
- `server/app/modules/tasks/drivers/base.py` — `CommitUncertainError` + `CommitGuard` + `NOOP_COMMIT_GUARD`
- `server/app/modules/tasks/drivers/__init__.py` — Protocol `publish()` 加 `commit_guard`/`retry_policy` 可选参
- `server/app/modules/tasks/models.py` — `PublishRecord` 加 `commit_attempted_at` / `failure_kind`
- `server/app/modules/tasks/executor.py` — `_make_commit_guard` + `build_publish_runner_for_record` 装配 + `_finish_record_future` 分流 + `_mark_record_failed(failure_kind=...)`
- `server/app/modules/tasks/runner.py` — `run_publish` 加 `commit_guard`/`retry_policy` 转发 driver
- `server/app/modules/tasks/runner_api.py` — `run_publish_api` 加 `commit_guard`/`retry_policy` 转发 driver
- `server/app/modules/tasks/drivers/wechat_mp.py` — 上传步包 `retry_call`、`add_draft` 进 `commit_guard`
- `server/app/modules/tasks/drivers/toutiao.py` — 发布页 goto 包 `retry_call`、确认发布进 `commit_guard`
- `server/app/modules/tasks/service.py` — `recover_stuck_records` + `retry_record(force=...)` 护栏
- `server/app/modules/tasks/router.py` — retry 端点加 `force` 查询参
- `server/app/modules/tasks/schemas.py` — `PublishRecordRead` 加 `failure_kind`，`to_record_read` 映射
- `web/src/features/tasks/*` + `web/src/api/tasks.ts` — `failure_kind=commit_uncertain` 渲染告警 + 禁用一键重试
- `server/tests/test_wechat_mp_retry.py` / `test_publish_commit_uncertain.py` / `test_recover_retry_guard.py` — 集成测

---

## Task 1: 通用退避重试核心 `shared/resilience.py`

**Files:**
- Create: `server/app/shared/resilience.py`
- Test: `server/tests/test_resilience.py`

**Interfaces:**
- Produces:
  - `RetryPolicy(enabled, max_attempts, base_delay, multiplier, max_delay, jitter, max_elapsed)` 冻结 dataclass
  - `default_is_transient(exc: BaseException) -> bool`
  - `retry_call(fn, *, policy, is_transient=default_is_transient, on_retry=None, sleeper=time.sleep, monotonic=time.monotonic, rand=random.random) -> T`

- [ ] **Step 1: 写失败测试**

```python
# server/tests/test_resilience.py
import httpx
import pytest

from server.app.shared.resilience import RetryPolicy, default_is_transient, retry_call


def test_retries_transient_then_succeeds():
    calls = {"n": 0}
    delays = []

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadTimeout("blip")
        return "ok"

    out = retry_call(
        fn,
        policy=RetryPolicy(max_attempts=3, base_delay=1.0, jitter=0.0),
        sleeper=delays.append,
        monotonic=lambda: 0.0,
    )
    assert out == "ok"
    assert calls["n"] == 3
    assert delays == [1.0, 2.0]  # base, base*multiplier


def test_permanent_not_retried():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ValueError("logic")

    with pytest.raises(ValueError):
        retry_call(fn, policy=RetryPolicy(max_attempts=5), sleeper=lambda d: None)
    assert calls["n"] == 1


def test_exhausts_and_raises_last():
    def fn():
        raise httpx.ConnectError("down")

    with pytest.raises(httpx.ConnectError):
        retry_call(
            fn,
            policy=RetryPolicy(max_attempts=2, base_delay=0.5, jitter=0.0),
            sleeper=lambda d: None,
            monotonic=lambda: 0.0,
        )


def test_max_elapsed_cuts_off_before_sleeping():
    clock = {"t": 0.0}

    def fn():
        clock["t"] += 40.0  # 每次调用推进 40s
        raise httpx.ReadTimeout("slow")

    with pytest.raises(httpx.ReadTimeout):
        retry_call(
            fn,
            policy=RetryPolicy(max_attempts=5, base_delay=1.0, jitter=0.0, max_elapsed=60.0),
            sleeper=lambda d: None,
            monotonic=lambda: clock["t"],
        )


def test_disabled_policy_calls_once():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise httpx.ReadTimeout("x")

    with pytest.raises(httpx.ReadTimeout):
        retry_call(fn, policy=RetryPolicy(enabled=False), sleeper=lambda d: None)
    assert calls["n"] == 1


def test_classifier_httpx_and_playwright():
    assert default_is_transient(httpx.ConnectError("x")) is True
    assert default_is_transient(httpx.ReadTimeout("x")) is True
    assert default_is_transient(ValueError("x")) is False

    class _FakePwTimeout(Exception):
        pass

    _FakePwTimeout.__module__ = "playwright._impl._errors"
    _FakePwTimeout.__name__ = "TimeoutError"
    assert default_is_transient(_FakePwTimeout()) is True
```

- [ ] **Step 2: 运行确认失败**

Run: `conda activate geo_xzpt && pytest server/tests/test_resilience.py -q`
Expected: FAIL（`ModuleNotFoundError: server.app.shared.resilience`）

- [ ] **Step 3: 实现**

```python
# server/app/shared/resilience.py
"""通用退避重试：平台无关、零 ORM。供发布链路（及未来 hot_lists/feishu/litellm）复用。

retry_call 只对 is_transient(exc)==True 的异常退避重试；其余立即抛。
不在此判定「提交边界是否安全」——那是 drivers.base.CommitGuard 的职责。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    enabled: bool = True
    max_attempts: int = 3  # 含首次
    base_delay: float = 1.0
    multiplier: float = 2.0
    max_delay: float = 15.0
    jitter: float = 0.2  # ±比例对称抖动，打散并发重试
    max_elapsed: float | None = 60.0


def default_is_transient(exc: BaseException) -> bool:
    """httpx 网络异常 / playwright 导航超时 / HTTP 5xx·429 视为可重试；其余永久。

    用类型 module+name 字符串判定，避免在 shared 层硬 import httpx/playwright。
    """
    mod = type(exc).__module__ or ""
    name = type(exc).__name__
    if mod.startswith("httpx") and name in {
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ReadError",
        "WriteError",
        "NetworkError",
        "RemoteProtocolError",
        "ProxyError",
    }:
        return True
    if mod.startswith("playwright") and name == "TimeoutError":
        return True
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) in {429, 500, 502, 503, 504}:
        return True
    return False


def _backoff_delay(policy: RetryPolicy, attempt: int, rand: Callable[[], float]) -> float:
    """attempt 从 1 开始（首次失败后的等待）。指数退避 + 对称抖动，封顶 max_delay。"""
    raw = min(policy.base_delay * (policy.multiplier ** (attempt - 1)), policy.max_delay)
    if policy.jitter:
        raw = raw * (1 + policy.jitter * (2 * rand() - 1))
    return max(0.0, raw)


def retry_call(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy,
    is_transient: Callable[[BaseException], bool] = default_is_transient,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    rand: Callable[[], float] = random.random,
) -> T:
    """同步退避重试。permanent 或 enabled=False / max_attempts<=1 → 不重试，原样抛。

    达到 max_attempts、命中 permanent，或下一次退避会越过 max_elapsed → 抛最后一次异常。
    """
    if not policy.enabled or policy.max_attempts <= 1:
        return fn()
    start = monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001 - 由 is_transient 决定是否吞
            if attempt >= policy.max_attempts or not is_transient(exc):
                raise
            delay = _backoff_delay(policy, attempt, rand)
            if policy.max_elapsed is not None and (monotonic() - start) + delay > policy.max_elapsed:
                raise
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleeper(delay)
```

- [ ] **Step 4: 运行确认通过**

Run: `conda activate geo_xzpt && pytest server/tests/test_resilience.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add server/app/shared/resilience.py server/tests/test_resilience.py
git commit -m "feat(resilience): 通用退避重试核心 RetryPolicy + retry_call"
```

---

## Task 2: 重试配置 `config.py`

**Files:**
- Modify: `server/app/core/config.py:54`（在 `publish_record_timeout_seconds` 附近加字段）+ 文件内加 `get_publish_retry_policy()`
- Test: `server/tests/test_resilience.py`（追加一个配置→policy 的用例）

**Interfaces:**
- Consumes: `RetryPolicy`（Task 1）
- Produces: `Settings.publish_retry_enabled/max_attempts/base_delay_seconds/max_delay_seconds/max_elapsed_seconds`；`get_publish_retry_policy() -> RetryPolicy`

- [ ] **Step 1: 写失败测试（追加到 test_resilience.py 末尾）**

```python
def test_get_publish_retry_policy_reads_settings(monkeypatch):
    from server.app.core.config import get_publish_retry_policy, get_settings

    monkeypatch.setenv("GEO_PUBLISH_RETRY_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("GEO_PUBLISH_RETRY_BASE_DELAY_SECONDS", "2.5")
    get_settings.cache_clear()
    policy = get_publish_retry_policy()
    assert policy.max_attempts == 5
    assert policy.base_delay == 2.5
    get_settings.cache_clear()
```

- [ ] **Step 2: 运行确认失败**

Run: `conda activate geo_xzpt && pytest server/tests/test_resilience.py::test_get_publish_retry_policy_reads_settings -q`
Expected: FAIL（`ImportError: cannot import name 'get_publish_retry_policy'`）

- [ ] **Step 3: 实现** — 在 `server/app/core/config.py` 的 `Settings` 类里 `publish_record_timeout_seconds: int = 300` 之后插入：

```python
    # 断网/弱网发布重试（见 docs/superpowers/specs/2026-06-23-publish-network-retry-design.md）
    publish_retry_enabled: bool = True  # GEO_PUBLISH_RETRY_ENABLED
    publish_retry_max_attempts: int = 3  # GEO_PUBLISH_RETRY_MAX_ATTEMPTS（含首次）
    publish_retry_base_delay_seconds: float = 1.0  # GEO_PUBLISH_RETRY_BASE_DELAY_SECONDS
    publish_retry_max_delay_seconds: float = 15.0  # GEO_PUBLISH_RETRY_MAX_DELAY_SECONDS
    publish_retry_max_elapsed_seconds: float = 60.0  # GEO_PUBLISH_RETRY_MAX_ELAPSED_SECONDS
```

在 `config.py` 文件**末尾**（`get_settings()` 定义之后）加：

```python
def get_publish_retry_policy() -> "RetryPolicy":
    """由 Settings 构建发布重试策略。注意：max_elapsed 必须 < 单记录执行预算
    （publish_record_timeout_seconds），否则 watchdog 会先于重试杀掉记录。"""
    from server.app.shared.resilience import RetryPolicy

    s = get_settings()
    return RetryPolicy(
        enabled=s.publish_retry_enabled,
        max_attempts=s.publish_retry_max_attempts,
        base_delay=s.publish_retry_base_delay_seconds,
        max_delay=s.publish_retry_max_delay_seconds,
        max_elapsed=s.publish_retry_max_elapsed_seconds,
    )
```

> `RetryPolicy` 在函数内懒导入，避免 `config` ↔ `shared` 任何潜在导入顺序问题（`shared.resilience` 不依赖 `config`，无真实环路，懒导入仅为稳妥）。

- [ ] **Step 4: 运行确认通过**

Run: `conda activate geo_xzpt && pytest server/tests/test_resilience.py -q`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
git add server/app/core/config.py server/tests/test_resilience.py
git commit -m "feat(config): GEO_PUBLISH_RETRY_* 设置 + get_publish_retry_policy"
```

---

## Task 3: 提交边界原语 `CommitGuard` / `CommitUncertainError`

**Files:**
- Modify: `server/app/modules/tasks/drivers/base.py`（文件末尾追加）
- Test: `server/tests/test_commit_guard.py`

**Interfaces:**
- Produces:
  - `CommitUncertainError(PublishError)`
  - `CommitGuard(mark_pending: Callable[[], None])`，方法 `committing()`（contextmanager）
  - `NOOP_COMMIT_GUARD: CommitGuard`

- [ ] **Step 1: 写失败测试**

```python
# server/tests/test_commit_guard.py
import httpx
import pytest

from server.app.modules.tasks.drivers.base import (
    CommitGuard,
    CommitUncertainError,
    PublishError,
)


def _guard(flag):
    return CommitGuard(mark_pending=lambda: flag.__setitem__("marked", True))


def test_marks_pending_on_enter():
    flag = {"marked": False}
    with _guard(flag).committing():
        assert flag["marked"] is True


def test_success_passes_through():
    flag = {"marked": False}
    with _guard(flag).committing():
        pass  # 无异常


def test_read_timeout_becomes_uncertain():
    flag = {"marked": False}
    with pytest.raises(CommitUncertainError):
        with _guard(flag).committing():
            raise httpx.ReadTimeout("response lost")


def test_connect_error_is_clean_failure():
    """连接从未建立 → 请求从未发出 → 干净失败，原异常透出（可安全重试）。"""
    flag = {"marked": False}
    with pytest.raises(httpx.ConnectError):
        with _guard(flag).committing():
            raise httpx.ConnectError("never connected")


def test_business_errcode_is_clean_failure():
    """服务端回了非空错误码 → 必定未受理 → 原异常透出。"""

    class _ApiErr(PublishError):
        def __init__(self):
            super().__init__("err 40164")
            self.errcode = 40164

    flag = {"marked": False}
    with pytest.raises(_ApiErr):
        with _guard(flag).committing():
            raise _ApiErr()


def test_unknown_exception_defaults_uncertain():
    """无正面「未受理」证据 → 保守判 uncertain（at-most-once）。"""
    flag = {"marked": False}
    with pytest.raises(CommitUncertainError):
        with _guard(flag).committing():
            raise RuntimeError("toutiao click timed out")
```

- [ ] **Step 2: 运行确认失败**

Run: `conda activate geo_xzpt && pytest server/tests/test_commit_guard.py -q`
Expected: FAIL（`ImportError: cannot import name 'CommitGuard'`）

- [ ] **Step 3: 实现** — 在 `server/app/modules/tasks/drivers/base.py` 末尾追加（顶部 import 区补 `from collections.abc import Callable`、`from contextlib import contextmanager`、`from collections.abc import Iterator`）：

```python
class CommitUncertainError(PublishError):
    """跨提交点后发生网络失败：请求已发出、平台是否受理未知。绝不自动重发（at-most-once）。"""


def _walk_exc(exc: BaseException) -> "Iterator[BaseException]":
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


def _commit_is_clean_failure(exc: BaseException) -> bool:
    """True = 有正面证据平台未受理（干净失败，原异常透出，可安全重试）。
    False = 结果未知，包成 CommitUncertainError。at-most-once 默认保守取 False。"""
    # 业务级拒绝：服务端回了非空错误码 → 必定未建记录
    if getattr(exc, "errcode", None) is not None:
        return True
    for e in _walk_exc(exc):
        mod = type(e).__module__ or ""
        name = type(e).__name__
        if mod.startswith("httpx"):
            if name in {"ConnectError", "ConnectTimeout"}:
                return True  # 连接从未建立 → 请求从未发出
            if name in {
                "ReadTimeout",
                "WriteTimeout",
                "PoolTimeout",
                "RemoteProtocolError",
                "ReadError",
                "WriteError",
                "NetworkError",
            }:
                return False  # 已发出或可能已发出 → 未知
    return False


class CommitGuard:
    """把不可逆提交包进 committing()。进入时落 commit_attempted_at（经 runner 注入的回调，
    驱动不碰 ORM）；退出时按异常性质分流为干净失败 or CommitUncertainError。"""

    def __init__(self, mark_pending: Callable[[], None]):
        self._mark_pending = mark_pending

    @contextmanager
    def committing(self) -> "Iterator[None]":
        self._mark_pending()
        try:
            yield
        except CommitUncertainError:
            raise
        except BaseException as exc:  # noqa: BLE001
            if _commit_is_clean_failure(exc):
                raise
            raise CommitUncertainError(
                f"提交后网络中断，平台受理结果未知: {exc}",
                screenshot=getattr(exc, "screenshot", None),
            ) from exc


NOOP_COMMIT_GUARD = CommitGuard(mark_pending=lambda: None)
```

- [ ] **Step 4: 运行确认通过**

Run: `conda activate geo_xzpt && pytest server/tests/test_commit_guard.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/tasks/drivers/base.py server/tests/test_commit_guard.py
git commit -m "feat(drivers): CommitGuard + CommitUncertainError 提交边界原语"
```

---

## Task 4: `PublishRecord` 加标记列 + 迁移

**Files:**
- Modify: `server/app/modules/tasks/models.py:119-123`（在 `lease_until` 附近加两列）
- Create: `server/alembic/versions/<rev>_publish_record_commit_markers.py`
- Test: `server/tests/test_publish_record_commit_markers.py`

**Interfaces:**
- Produces: `PublishRecord.commit_attempted_at: datetime | None`、`PublishRecord.failure_kind: str | None`

- [ ] **Step 1: 写失败测试**

> **造数模式（真实，照搬 `server/tests/test_account_reconcile.py:60-85`）**：仓库无通用 record 工厂；DB 测试自建最小 `User+Platform+Account+Article+PublishTask+PublishRecord`。本计划在每个 DB 测试文件顶部放一个 `_seed_record` helper：

```python
def _seed_record(db, *, status="pending", username="op"):
    """最小造一条 PublishRecord（连带 user/platform/account/article/task），返回 (task, record)。"""
    from server.app.modules.accounts.models import Account
    from server.app.modules.articles.models import Article
    from server.app.modules.system.models import Platform, User
    from server.app.modules.tasks.models import PublishRecord, PublishTask

    user = User(username=username, role="operator", is_active=True, must_change_password=False)
    user.set_password("pw-123456")
    db.add(user)
    db.flush()
    platform = Platform(code="toutiao", name="头条号", base_url="https://mp.toutiao.com", enabled=True)
    db.add(platform)
    db.flush()
    account = Account(
        user_id=user.id,
        platform_id=platform.id,
        display_name="acc",
        platform_user_id=None,
        status="valid",
        state_path="browser_states/toutiao/acc/storage_state.json",
    )
    db.add(account)
    db.flush()
    article = Article(user_id=user.id, title="t", status="ready")
    db.add(article)
    db.flush()
    task = PublishTask(
        user_id=user.id, name="task", task_type="single",
        platform_id=platform.id, article_id=article.id,
    )
    db.add(task)
    db.flush()
    record = PublishRecord(
        task_id=task.id, article_id=article.id,
        platform_id=platform.id, account_id=account.id, status=status,
    )
    db.add(record)
    db.flush()
    return task, record
```

```python
# server/tests/test_publish_record_commit_markers.py
import pytest

from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


# ... 此处粘贴上面的 _seed_record helper ...


def test_commit_marker_columns_roundtrip(monkeypatch):
    from server.app.core.time import utcnow
    from server.app.modules.tasks.models import PublishRecord

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            _task, rec = _seed_record(db)
            rec.commit_attempted_at = utcnow()
            rec.failure_kind = "commit_uncertain"
            db.commit()
            db.refresh(rec)
            assert rec.failure_kind == "commit_uncertain"
            assert rec.commit_attempted_at is not None
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 运行确认失败**

Run: `conda activate geo_xzpt && GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test pytest server/tests/test_publish_record_commit_markers.py -q`
Expected: FAIL（`AttributeError: 'PublishRecord' object has no attribute 'failure_kind'` 或迁移未含该列）

- [ ] **Step 3a: 改模型** — `server/app/modules/tasks/models.py`，在 `lease_until` 行后插入：

```python
    # 断网/弱网发布重试：跨提交点前置时间戳 + 失败归类（见 2026-06-23 spec）
    commit_attempted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failure_kind: Mapped[str | None] = mapped_column(String(40), nullable=True)
```

- [ ] **Step 3b: 生成迁移** — 先看当前 head（**用户拍板：本迁移后于加密 spec 的 accounts 迁移**）：

```bash
ls server/alembic/versions/    # 确认最新一个文件即 down_revision
conda activate geo_xzpt && alembic revision -m "publish_record_commit_markers"
```

把生成文件的 `down_revision` 设为上一步看到的当前 head，`upgrade()` / `downgrade()` 填：

```python
def upgrade() -> None:
    op.add_column("publish_records", sa.Column("commit_attempted_at", sa.DateTime(), nullable=True))
    op.add_column("publish_records", sa.Column("failure_kind", sa.String(length=40), nullable=True))


def downgrade() -> None:
    op.drop_column("publish_records", "failure_kind")
    op.drop_column("publish_records", "commit_attempted_at")
```

> 追加式、**不改 `ck_publish_records_status` CHECK 约束**。两列均 nullable，无 server_default，老行读出 NULL。

- [ ] **Step 4: 运行确认通过**

Run: `conda activate geo_xzpt && GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test pytest server/tests/test_publish_record_commit_markers.py -q`
Expected: PASS（1 passed）

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/tasks/models.py server/alembic/versions/*_publish_record_commit_markers.py server/tests/test_publish_record_commit_markers.py
git commit -m "feat(models): PublishRecord 加 commit_attempted_at / failure_kind + 迁移"
```

---

## Task 5: CommitGuard 装配（executor + runner 转发，默认 no-op 不改行为）

**Files:**
- Modify: `server/app/modules/tasks/executor.py`（加 `_make_commit_guard`；`build_publish_runner_for_record` 装配）
- Modify: `server/app/modules/tasks/runner.py:287-380`（`run_publish` 加 `commit_guard`/`retry_policy` 转发 `driver.publish`）
- Modify: `server/app/modules/tasks/runner_api.py:123-144`（`run_publish_api` 加 `commit_guard`/`retry_policy` 转发 `driver.publish_api`）
- Modify: `server/app/modules/tasks/drivers/__init__.py:29-37`（Protocol `publish()` 加可选参）
- Test: `server/tests/test_commit_guard_wiring.py`

**Interfaces:**
- Consumes: `CommitGuard`/`NOOP_COMMIT_GUARD`（Task 3）、`get_publish_retry_policy`（Task 2）、`commit_attempted_at` 列（Task 4）
- Produces: `executor._make_commit_guard(record_id) -> CommitGuard`；`run_publish(..., commit_guard=NOOP_COMMIT_GUARD, retry_policy=None)`；`run_publish_api(..., commit_guard=NOOP_COMMIT_GUARD, retry_policy=None)`；驱动 `publish`/`publish_api` 接受 `commit_guard`/`retry_policy`

- [ ] **Step 1: 写失败测试** — 断言 `_make_commit_guard(rid).committing()` 进入时会把该 record 的 `commit_attempted_at` 落库：

```python
# server/tests/test_commit_guard_wiring.py
import pytest

from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


# ... 粘贴 Task 4 的 _seed_record helper ...


def test_make_commit_guard_marks_record(monkeypatch):
    from server.app.modules.tasks.executor import _make_commit_guard
    from server.app.modules.tasks.models import PublishRecord

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            _task, rec = _seed_record(db)
            db.commit()
            rid = rec.id

        # _make_commit_guard 的 mark_pending 自开 SessionLocal（被 build_test_app monkeypatch 指向测试库）
        guard = _make_commit_guard(rid)
        with guard.committing():
            pass

        with test_app.session_factory() as db2:
            refreshed = db2.get(PublishRecord, rid)
            assert refreshed.commit_attempted_at is not None
    finally:
        test_app.cleanup()
```

> `build_test_app` 已 `monkeypatch.setattr("server.app.db.session.SessionLocal", TestingSessionLocal)`（见 `utils.py:204`），故 `_mark_pending` 内 `from ...db.session import SessionLocal` 拿到的是测试库 session，写入对测试可见。

- [ ] **Step 2: 运行确认失败**

Run: `conda activate geo_xzpt && GEO_TEST_DATABASE_URL=...geo_test pytest server/tests/test_commit_guard_wiring.py -q`
Expected: FAIL（`ImportError: cannot import name '_make_commit_guard'`）

- [ ] **Step 3a: executor 加 `_make_commit_guard`** — 在 `server/app/modules/tasks/executor.py` 的 `build_publish_runner_for_record` 之前插入：

```python
def _make_commit_guard(record_id: int) -> "CommitGuard":
    """构造该记录的提交守卫：mark_pending 自开 session 落 commit_attempted_at（发布线程内，
    入参均 detached，不复用外部 session；与 runner_api._resolve_access_token 同模式）。"""
    from server.app.modules.tasks.drivers.base import CommitGuard

    def _mark_pending() -> None:
        from server.app.db.session import SessionLocal

        db = SessionLocal()
        try:
            db.execute(
                sa_update(PublishRecord)
                .where(
                    PublishRecord.id == record_id,
                    PublishRecord.is_deleted == False,  # noqa: E712
                )
                .values(commit_attempted_at=utcnow())
            )
            db.commit()
        finally:
            db.close()

    return CommitGuard(mark_pending=_mark_pending)
```

> `sa_update` / `utcnow` / `PublishRecord` 在 executor.py 顶部已 import（见 `_mark_record_failed`）。

- [ ] **Step 3b: `build_publish_runner_for_record` 装配** — 修改两个 runner 闭包，注入 guard + policy：

```python
    # 在函数体内、解析 platform_code 后构造（两分支共用）：
    commit_guard = _make_commit_guard(record.id)
    retry_policy = get_publish_retry_policy()
```

API 分支：

```python
        def _api_runner(article, account, *, stop_before_publish=False):
            return run_publish_api(
                article=article,
                account=account,
                driver=driver,
                platform_code=platform_code,
                commit_guard=commit_guard,
                retry_policy=retry_policy,
            )
```

浏览器分支：

```python
    def _runner(article, account, *, stop_before_publish=False):
        return run_publish(
            record_id=_record_id,
            article=article,
            account=account,
            channel=channel,
            executable_path=executable_path,
            stop_before_publish=stop_before_publish,
            commit_guard=commit_guard,
            retry_policy=retry_policy,
        )
```

> `get_publish_retry_policy` 在 executor.py 顶部 import 区补：`from server.app.core.config import get_publish_retry_policy`（`get_settings` 已 import，同模块）。

- [ ] **Step 3c: `run_publish_api` 转发** — `server/app/modules/tasks/runner_api.py`：

```python
def run_publish_api(
    *,
    article: Article,
    account: Account,
    driver,
    platform_code: str,
    commit_guard=None,
    retry_policy=None,
) -> PublishResult:
    from server.app.modules.tasks.drivers.base import NOOP_COMMIT_GUARD
    from server.app.modules.tasks.runner import _cleanup_temp_files

    if commit_guard is None:
        commit_guard = NOOP_COMMIT_GUARD
    # ...（原校验 / token / payload 不变）...
    try:
        with publish_step("api driver publish flow"):
            return driver.publish_api(
                payload=payload, commit_guard=commit_guard, retry_policy=retry_policy
            )
    finally:
        _cleanup_temp_files(payload.temp_files)
```

- [ ] **Step 3d: `run_publish` 转发** — `server/app/modules/tasks/runner.py`，函数签名加 `commit_guard=None, retry_policy=None`，函数体顶部 `if commit_guard is None: from ...base import NOOP_COMMIT_GUARD; commit_guard = NOOP_COMMIT_GUARD`，并把 `driver.publish(...)` 调用改为：

```python
            result = driver.publish(
                page=page,
                context=context,
                payload=payload,
                stop_before_publish=stop_before_publish,
                commit_guard=commit_guard,
                retry_policy=retry_policy,
            )
```

- [ ] **Step 3e: Protocol 加可选参** — `server/app/modules/tasks/drivers/__init__.py` 的 `PlatformDriver.publish`：

```python
    def publish(
        self,
        *,
        page: Page,
        context: BrowserContext,
        payload: PublishPayload,
        stop_before_publish: bool,
        commit_guard=None,
        retry_policy=None,
    ) -> PublishResult:
        """填写表单、上传资源并点击发布；不负责浏览器生命周期。
        commit_guard/retry_policy 可选：不接入弹性的驱动忽略即可。"""
```

> 现有驱动（toutiao）此时尚未接受新参——Task 7 才改。为不破坏，**本 Task 先给 toutiao.publish 加 `**_kw` 吞掉**：把 `toutiao.py:1097` 的 `def publish(self, *, page, context, payload, stop_before_publish):` 改为 `def publish(self, *, page, context, payload, stop_before_publish, commit_guard=None, retry_policy=None):`（Task 7 再真正使用）。wechat 的 `publish_api` 在 Task 6 改。**本 Task 同步把 wechat `publish_api` 也加上 `commit_guard=None, retry_policy=None` 并忽略**，保证 Task 5 单独可跑通：

```python
    # wechat_mp.py：本 Task 仅扩签名、暂不使用（Task 6 接入）
    def publish_api(self, *, payload, client=None, commit_guard=None, retry_policy=None):
        ...
```

- [ ] **Step 4: 运行确认通过**

Run: `conda activate geo_xzpt && GEO_TEST_DATABASE_URL=...geo_test pytest server/tests/test_commit_guard_wiring.py -q && pytest server/tests/test_tasks_api.py -q`
Expected: PASS（装配测过；既有任务测零回归）

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/tasks/executor.py server/app/modules/tasks/runner.py server/app/modules/tasks/runner_api.py server/app/modules/tasks/drivers/__init__.py server/app/modules/tasks/drivers/toutiao.py server/app/modules/tasks/drivers/wechat_mp.py server/tests/test_commit_guard_wiring.py
git commit -m "feat(tasks): 装配 CommitGuard/retry_policy 到 runner 与驱动签名(默认 no-op)"
```

---

## Task 6: 微信 API 驱动接入（上传重试 + add_draft 提交守卫）

**Files:**
- Modify: `server/app/modules/tasks/drivers/wechat_mp.py`
- Test: `server/tests/test_wechat_mp_retry.py`

**Interfaces:**
- Consumes: `retry_call`/`RetryPolicy`（Task 1）、`CommitGuard`/`NOOP_COMMIT_GUARD`（Task 3）
- Produces: `WeChatMpDriver.publish_api(*, payload, client=None, commit_guard=None, retry_policy=None)` 真正使用 guard + retry

- [ ] **Step 1: 写失败测试** — 用 `httpx.MockTransport`：上传接口前 2 次网络错、第 3 次成功（验证重试）；`draft/add` 注入连接中断（`ReadTimeout`）→ 断言抛 `CommitUncertainError` 且 mark_pending 被调用、`add_draft` 只发一次（未重试）：

```python
# server/tests/test_wechat_mp_retry.py
import httpx

from server.app.modules.articles.parser import BodySegment
from server.app.modules.tasks.drivers.base import CommitGuard, CommitUncertainError, NOOP_COMMIT_GUARD
from server.app.modules.tasks.drivers.wechat_mp import WeChatMpDriver
from server.app.modules.tasks.drivers.base import ApiPublishPayload
from server.app.shared.resilience import RetryPolicy


def _payload(tmp_path):
    img = tmp_path / "c.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 200)  # 最小 JPEG 头占位
    return ApiPublishPayload(
        title="t",
        body_segments=[BodySegment(kind="text", text="hi")],
        cover_path=img,
        display_name="acc",
        platform_code="wechat_mp",
        access_token="tok",
    )


def _ok(json_body):
    return httpx.Response(200, json=json_body)


def test_upload_retries_then_succeeds(tmp_path, monkeypatch):
    # 让图片压缩直通，避免依赖 Pillow 细节
    monkeypatch.setattr(
        "server.app.modules.tasks.drivers.wechat_mp.compress_cover_to_jpeg", lambda b: b
    )
    calls = {"thumb": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "add_material" in url:
            calls["thumb"] += 1
            if calls["thumb"] < 3:
                raise httpx.ReadTimeout("blip", request=request)
            return _ok({"media_id": "m1"})
        if "draft/add" in url:
            return _ok({"media_id": "draft1"})
        return _ok({})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    driver = WeChatMpDriver()
    result = driver.publish_api(
        payload=_payload(tmp_path),
        client=client,
        commit_guard=NOOP_COMMIT_GUARD,
        retry_policy=RetryPolicy(max_attempts=3, base_delay=0.0, jitter=0.0, max_elapsed=None),
    )
    assert "draft1" in result.message
    assert calls["thumb"] == 3


def test_add_draft_network_loss_is_commit_uncertain(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "server.app.modules.tasks.drivers.wechat_mp.compress_cover_to_jpeg", lambda b: b
    )
    marked = {"n": 0}
    draft_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "add_material" in url:
            return _ok({"media_id": "m1"})
        if "draft/add" in url:
            draft_calls["n"] += 1
            raise httpx.ReadTimeout("response lost", request=request)
        return _ok({})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    guard = CommitGuard(mark_pending=lambda: marked.__setitem__("n", marked["n"] + 1))
    driver = WeChatMpDriver()
    try:
        driver.publish_api(
            payload=_payload(tmp_path),
            client=client,
            commit_guard=guard,
            retry_policy=RetryPolicy(max_attempts=3, base_delay=0.0, jitter=0.0, max_elapsed=None),
        )
        assert False, "应抛 CommitUncertainError"
    except CommitUncertainError:
        pass
    assert marked["n"] == 1  # 进守卫前标记一次
    assert draft_calls["n"] == 1  # add_draft 未重试


def test_business_errcode_stays_publish_error(tmp_path, monkeypatch):
    from server.app.modules.tasks.drivers.base import PublishError

    monkeypatch.setattr(
        "server.app.modules.tasks.drivers.wechat_mp.compress_cover_to_jpeg", lambda b: b
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "add_material" in url:
            return _ok({"media_id": "m1"})
        if "draft/add" in url:
            return _ok({"errcode": 40164, "errmsg": "ip not in whitelist"})
        return _ok({})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    driver = WeChatMpDriver()
    try:
        driver.publish_api(
            payload=_payload(tmp_path),
            client=client,
            commit_guard=CommitGuard(mark_pending=lambda: None),
            retry_policy=RetryPolicy(max_attempts=3, base_delay=0.0, jitter=0.0, max_elapsed=None),
        )
        assert False
    except CommitUncertainError:
        assert False, "业务错误码不应判为 uncertain"
    except PublishError:
        pass  # 干净失败
```

- [ ] **Step 2: 运行确认失败**

Run: `conda activate geo_xzpt && pytest server/tests/test_wechat_mp_retry.py -q`
Expected: FAIL（`publish_api()` 尚未使用 guard/retry → 第二个用例不抛 CommitUncertainError）

- [ ] **Step 3: 实现** — 改 `server/app/modules/tasks/drivers/wechat_mp.py`：

顶部 import 补：

```python
from server.app.modules.tasks.drivers.base import NOOP_COMMIT_GUARD
from server.app.shared.resilience import RetryPolicy, retry_call
```

加驱动级分类器（WeChat 把 httpx 错误塌缩成 `WeChatApiError(errcode=None)`，见 `wechat_client._request`）：

```python
def _wechat_is_transient(exc: BaseException) -> bool:
    """WeChatApiError(errcode=None)=网络不可达(见 wechat_client._request)→可重试；
    errcode 非空=业务错误→永久不重试。"""
    if isinstance(exc, WeChatApiError):
        return exc.errcode is None
    return False
```

改 `publish_api` / `_publish_api`：

```python
    def publish_api(
        self, *, payload, client=None, commit_guard=None, retry_policy=None
    ):
        if commit_guard is None:
            commit_guard = NOOP_COMMIT_GUARD
        policy = retry_policy or RetryPolicy()
        owns_client = client is None
        if client is None:
            client = make_default_client()
        try:
            return self._publish_api(
                payload=payload, client=client, commit_guard=commit_guard, policy=policy
            )
        except WeChatApiError as exc:
            raise PublishError(str(exc)) from exc
        finally:
            if owns_client:
                client.close()

    def _publish_api(self, *, payload, client, commit_guard, policy) -> PublishResult:
        token = payload.access_token
        cover_path = payload.cover_path
        if cover_path is None:
            cover_path = next(
                (s.image_path for s in payload.body_segments if s.kind == "image" and s.image_path),
                None,
            )
        if cover_path is None:
            raise PublishError("公众号草稿需要封面图（或正文至少一张图）")

        thumb_media_id = retry_call(
            lambda: upload_thumb(
                token, "cover.jpg", compress_cover_to_jpeg(cover_path.read_bytes()), client=client
            ),
            policy=policy,
            is_transient=_wechat_is_transient,
        )

        image_urls: dict[int, str] = {}
        for index, seg in enumerate(payload.body_segments):
            if seg.kind != "image" or seg.image_path is None:
                continue
            data, filename = compress_content_image(seg.image_path.read_bytes(), seg.image_path.name)
            image_urls[index] = retry_call(
                lambda data=data, filename=filename: upload_content_image(
                    token, filename, data, client=client
                ),
                policy=policy,
                is_transient=_wechat_is_transient,
            )

        content_html = segments_to_html(payload.body_segments, image_urls)
        if not content_html:
            raise PublishError("正文为空，无法创建公众号草稿")
        article = build_draft_article(
            title=payload.title, content_html=content_html, thumb_media_id=thumb_media_id
        )
        # 提交边界：add_draft 非幂等，不进 retry_call，只进 commit_guard
        with commit_guard.committing():
            media_id = add_draft(token, article, client=client)
        return PublishResult(
            url=None,
            title=payload.title,
            message=f"草稿已写入公众号草稿箱 media_id={media_id}",
        )
```

> 注意 lambda 闭包变量绑定：上传图用 `data=data, filename=filename` 默认参绑定，避免循环变量延迟绑定 bug。

- [ ] **Step 4: 运行确认通过**

Run: `conda activate geo_xzpt && pytest server/tests/test_wechat_mp_retry.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/tasks/drivers/wechat_mp.py server/tests/test_wechat_mp_retry.py
git commit -m "feat(wechat): 上传步退避重试 + add_draft 提交守卫(at-most-once)"
```

---

## Task 7: 头条浏览器驱动接入（发布页 goto 重试 + 确认发布提交守卫）

**Files:**
- Modify: `server/app/modules/tasks/drivers/toutiao.py`（`_do_publish` 透传 guard/policy；`_click_publish_and_wait` 把确认发布段进守卫；发布页 goto 包重试）
- Test: `server/tests/test_toutiao_commit_guard.py`（用 fake page 对象，无真实浏览器）

**Interfaces:**
- Consumes: `CommitGuard`/`NOOP_COMMIT_GUARD`（Task 3）、`retry_call`/`RetryPolicy`（Task 1）
- Produces: `toutiao.publish(..., commit_guard=None, retry_policy=None)` 真正使用；`_click_publish_and_wait(page, stop_before_publish, commit_guard)`

- [ ] **Step 1: 写失败测试** — fake page：`get_by_role("确认发布").click()` 后的 `wait_for_url` 抛超时 → 断言 `_click_publish_and_wait` 在 commit_guard 内抛 `CommitUncertainError`：

```python
# server/tests/test_toutiao_commit_guard.py
import pytest

from server.app.modules.tasks.drivers.base import CommitGuard, CommitUncertainError
from server.app.modules.tasks.drivers.toutiao import _click_publish_and_wait


class _Btn:
    def __init__(self, page, fail=False):
        self._page = page
        self._fail = fail

    def wait_for(self, **_):
        pass

    def click(self):
        if self._fail:
            raise TimeoutError("network lost during confirm click")  # 模拟点确认发布时断网
        self._page.clicked.append("preview")


class _FakePage:
    """最小桩：「预览并发布」点击成功；「确认发布」点击抛网络超时（提交边界处断网）。"""

    def __init__(self):
        self.url = "https://mp.toutiao.com/profile_v4/graphic/publish"
        self.clicked = []

    def get_by_role(self, role, name=None):
        return _Btn(self, fail=(name == "确认发布"))

    def wait_for_timeout(self, _ms):
        pass


def test_confirm_click_network_loss_is_uncertain(monkeypatch):
    # 关闭弹窗 / 截图 / 正文摘要等噪声（确认发布失败分支会调用）
    monkeypatch.setattr(
        "server.app.modules.tasks.drivers.toutiao._dismiss_blocking_popups", lambda p: None
    )
    monkeypatch.setattr("server.app.modules.tasks.drivers.toutiao._screenshot", lambda p: None)
    monkeypatch.setattr("server.app.modules.tasks.drivers.toutiao._body_text_hint", lambda p: "")

    marked = {"n": 0}
    guard = CommitGuard(mark_pending=lambda: marked.__setitem__("n", marked["n"] + 1))
    page = _FakePage()
    with pytest.raises(CommitUncertainError):
        _click_publish_and_wait(page, stop_before_publish=False, commit_guard=guard)
    assert marked["n"] == 1  # 进守卫前标记一次
```

> 「确认发布」点击抛 `TimeoutError` → 被 toutiao 包成 `ToutiaoPublishError`（`__cause__=TimeoutError`，均非 httpx「未发出」证据、无 errcode）→ 守卫判 uncertain → `CommitUncertainError`。轮询段在守卫外、本用例走不到。

- [ ] **Step 2: 运行确认失败**

Run: `conda activate geo_xzpt && pytest server/tests/test_toutiao_commit_guard.py -q`
Expected: FAIL（`_click_publish_and_wait()` 不接受 `commit_guard` 参 / 不抛 CommitUncertainError）

- [ ] **Step 3a: `_click_publish_and_wait` 把「点确认发布」那一下进守卫（轮询检测保持原状）** — 改 `server/app/modules/tasks/drivers/toutiao.py:887`：

签名加 `commit_guard=None`；只把**不可逆的「确认发布」点击**包进守卫，**点击后的轮询检测段（原 914-948 行）原样保留、不进守卫**——避免把既有的「30s 未跳转→乐观判成功」改成 uncertain（那是对主力平台的实质行为变更，见下「⚠️ 已知残留与待决」）。结构改为：

```python
def _click_publish_and_wait(page, stop_before_publish=False, commit_guard=None):
    """两步发布：先点"预览并发布"，再点"确认发布"。「确认发布」点击=提交边界。"""
    from server.app.modules.tasks.drivers.base import NOOP_COMMIT_GUARD

    if commit_guard is None:
        commit_guard = NOOP_COMMIT_GUARD
    before_url = page.url

    try:
        _dismiss_blocking_popups(page)
        page.get_by_role("button", name="预览并发布").click()
    except Exception as exc:
        raise ToutiaoPublishError(f"无法点击「预览并发布」按钮: {exc}") from exc

    page.wait_for_timeout(300)
    _dismiss_blocking_popups(page)

    if stop_before_publish:
        return None

    # ── 提交边界：仅「确认发布」点击。点击因网络失败=可能已提交→结果未知 ──
    with commit_guard.committing():
        try:
            confirm_btn = page.get_by_role("button", name="确认发布")
            confirm_btn.wait_for(state="visible", timeout=30000)
            confirm_btn.click()
        except Exception as exc:
            body_hint = _body_text_hint(page)
            screenshot = _screenshot(page)
            raise ToutiaoPublishError(
                f"无法点击「确认发布」按钮: {exc}\n页面内容摘要: {body_hint}", screenshot
            ) from exc

    # ── 守卫之外：发布后轮询检测（原 914-948 行整段原样保留，不改一字）──
    page.wait_for_timeout(500)
    for attempt in range(6):
        _dismiss_blocking_popups(page)
        try:
            page.wait_for_url(lambda url: url != before_url, timeout=5000)
            return page.url
        except Exception:
            pass
        try:
            body_text = page.locator("body").inner_text(timeout=2000)
            if any(h in body_text for h in ("发布失败", "提交失败", "操作失败", "网络错误")):
                raise ToutiaoPublishError(f"发布页面报错: {body_text[:300]}")
            if any(h in body_text for h in ("发布成功", "已发布", "审核中", "投稿成功")):
                logger.info("Publish confirmed by page text after attempt %d", attempt + 1)
                return page.url
        except ToutiaoPublishError:
            raise
        except Exception:
            pass
    logger.warning("URL change wait failed after publish (all 6 attempts); treating as success")
    return page.url
```

> 语义：`确认发布` 点击因网络/超时失败（守卫内）→ 无 errcode、非 httpx「未发出」证据 → 包成 `CommitUncertainError`（正确：点击可能已生效）。点击成功后的轮询检测**完全保持现状**——包括「发布失败」文字→`ToutiaoPublishError`（守卫外抛出 → 仍是普通 `PublishError`=干净失败，可重试，正确）和「30s 未跳转→乐观判成功」。
>
> **⚠️ 已知残留与待决（实现者/需求方注意）**：点击成功**之后**、轮询期间网络中断 → 现有「treating as success」会把它判成 succeeded（既有行为，本改动**不触碰**）。若需求方要求「轮询期断网也判 uncertain」，需另做决定：把 945-948 的乐观兜底从 `return page.url` 改为 `raise ToutiaoPublishError("发布后无法确认结果")` 并**把轮询也纳入守卫**——代价是每次「慢但成功」的头条发布都会变成 `commit_uncertain` 需人工核对，噪声较大。**本计划默认不改**（保守=不引入行为回归）；此决定已在交付时单独抛给需求方。

- [ ] **Step 3b: `_do_publish` 透传 + 发布页 goto 重试** — `toutiao.py:1036` 的 `def _do_publish(page, context, payload, stop_before_publish)` 加 `commit_guard=None, retry_policy=None`；点发布调用处（`:1069`）`publish_url = _click_publish_and_wait(page, stop_before_publish)` 改为 `_click_publish_and_wait(page, stop_before_publish, commit_guard=commit_guard)`。

发布页 goto 重试——**必须替换 `:1046-1047` 既有的那一块**（不是新增一块），且**原样保留 `wait_until="domcontentloaded"`**（丢了它=改导航语义=对头条的行为回归）。现有：

```python
    with publish_step("open Toutiao publish page", page=page):
        page.goto(TOUTIAO_PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
```

改为：

```python
    from server.app.shared.resilience import RetryPolicy, retry_call

    _policy = retry_policy or RetryPolicy()
    with publish_step("open Toutiao publish page", page=page):
        retry_call(
            lambda: page.goto(TOUTIAO_PUBLISH_URL, wait_until="domcontentloaded", timeout=60000),
            policy=_policy,
        )
```

> 用默认 `default_is_transient`（识别 playwright `TimeoutError`）。导航幂等、重放安全。`import` 置于函数内或文件顶部均可（toutiao.py 顶部已有大量 import，加到顶部更整洁）。

- [ ] **Step 3c: `publish()` 透传** — `toutiao.py:1097` 的 `def publish(self, *, page, context, payload, stop_before_publish, commit_guard=None, retry_policy=None):`（Task 5 已扩签名）函数体：`return _do_publish(page, context, payload, stop_before_publish, commit_guard=commit_guard, retry_policy=retry_policy)`。

- [ ] **Step 4: 运行确认通过**

Run: `conda activate geo_xzpt && pytest server/tests/test_toutiao_commit_guard.py -q`
Expected: PASS（1 passed）

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/tasks/drivers/toutiao.py server/tests/test_toutiao_commit_guard.py
git commit -m "feat(toutiao): 确认发布提交守卫 + 发布页 goto 重试"
```

---

## Task 8: executor 分流 CommitUncertainError + failure_kind

**Files:**
- Modify: `server/app/modules/tasks/executor.py:949-974`（`_mark_record_failed` 加 `failure_kind`）+ `:820`（`_finish_record_future` 加分支）
- Test: `server/tests/test_publish_commit_uncertain.py`

**Interfaces:**
- Consumes: `CommitUncertainError`（Task 3）、`failure_kind` 列（Task 4）
- Produces: `_mark_record_failed(..., failure_kind: str | None = None)`；`_finish_record_future` 把 `CommitUncertainError` → `failed` + `failure_kind="commit_uncertain"`

- [ ] **Step 1: 写失败测试** — mock runner 抛 `CommitUncertainError`，跑 execute_task，断言记录 `status=failed` 且 `failure_kind=commit_uncertain`：

```python
# server/tests/test_publish_commit_uncertain.py
from concurrent.futures import Future

import pytest

from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


# ... 粘贴 Task 4 的 _seed_record helper ...


def test_commit_uncertain_marks_failure_kind(monkeypatch):
    """直接喂 _finish_record_future 一个抛 CommitUncertainError 的 future，断言落 failed+commit_uncertain。"""
    from server.app.modules.tasks.drivers.base import CommitUncertainError
    from server.app.modules.tasks.executor import _finish_record_future
    from server.app.modules.tasks.models import PublishRecord

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            task, rec = _seed_record(db, status="running")  # _mark_record_failed 条件 UPDATE 要求 running
            db.commit()
            rid = rec.id

            fut: Future = Future()
            fut.set_exception(CommitUncertainError("提交后断网"))
            _finish_record_future(db, task, rid, fut)
            db.commit()

            refreshed = db.get(PublishRecord, rid)
            assert refreshed.status == "failed"
            assert refreshed.failure_kind == "commit_uncertain"
    finally:
        test_app.cleanup()
```

> 用 `concurrent.futures.Future` + `set_exception`：`future.result()` 在 `_finish_record_future` 内重抛该异常，命中新增的 `CommitUncertainError` 分支，无需起线程池 / 真实驱动。`_store_failure_screenshot`(无截图→None) / `_stop_record_session`(无会话→安全) / `_add_publish_diagnostics`(空列表) 在测试环境均安全。

- [ ] **Step 2: 运行确认失败**

Run: `conda activate geo_xzpt && GEO_TEST_DATABASE_URL=...geo_test pytest server/tests/test_publish_commit_uncertain.py -q`
Expected: FAIL（`failure_kind` 仍为 None——executor 未分流）

- [ ] **Step 3a: `_mark_record_failed` 加 failure_kind** — `server/app/modules/tasks/executor.py:949`：

```python
def _mark_record_failed(
    db: Session,
    task_id: int,
    record_id: int,
    error_message: str,
    screenshot_asset_id: str | None = None,
    failure_kind: str | None = None,
) -> None:
    stmt = (
        sa_update(PublishRecord)
        .where(
            PublishRecord.id == record_id,
            PublishRecord.status == "running",
            PublishRecord.is_deleted == False,  # noqa: E712
        )
        .values(
            status="failed",
            error_message=error_message,
            failure_kind=failure_kind,
            finished_at=utcnow(),
            lease_until=None,
            queue_reason=None,
        )
    )
    if db.execute(stmt).rowcount > 0:  # type: ignore[attr-defined]
        add_log(
            db, task_id, record_id, "error", error_message, screenshot_asset_id=screenshot_asset_id
        )
```

- [ ] **Step 3b: `_finish_record_future` 加分支** — 在 `except UserInputRequired` 之后、`except PublishError` **之前**插入（顶部 import 补 `CommitUncertainError`）：

```python
    except CommitUncertainError as exc:
        try:
            _add_publish_diagnostics(
                db, task.id, record_id, _diagnostics_from_exception(exc), task.user_id
            )
            screenshot_asset_id = _store_failure_screenshot(
                db, task.id, record_id, exc.screenshot, task.user_id
            )
            _mark_record_failed(
                db,
                task.id,
                record_id,
                f"[结果未知·已提交，请人工核对平台] {exc}\n{traceback.format_exc()}",
                screenshot_asset_id=screenshot_asset_id,
                failure_kind="commit_uncertain",
            )
            _stop_record_session(record_id)
            _logger.error("Record %d commit-uncertain: %s", record_id, exc)
        except Exception as _inner:
            _logger.error(
                "Record %d: error handling CommitUncertain: %s", record_id, _inner, exc_info=True
            )
            _mark_record_failed(
                db, task.id, record_id, f"Error handling commit-uncertain: {_inner}"
            )
            _stop_record_session(record_id)
```

import：`from server.app.modules.tasks.drivers.base import CommitUncertainError`（executor 顶部已 import `PublishError`/`UserInputRequired`，同处补）。

- [ ] **Step 4: 运行确认通过**

Run: `conda activate geo_xzpt && GEO_TEST_DATABASE_URL=...geo_test pytest server/tests/test_publish_commit_uncertain.py -q`
Expected: PASS（1 passed）

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/tasks/executor.py server/tests/test_publish_commit_uncertain.py
git commit -m "feat(executor): CommitUncertainError 分流 → failed + failure_kind"
```

---

## Task 9: 记录层护栏（recover 不重跑 + retry 默认拦截 commit_uncertain + force）

**Files:**
- Modify: `server/app/modules/tasks/service.py:358-389`（`recover_stuck_records`）+ `:296-355`（`retry_record` 加 `force`）
- Modify: `server/app/modules/tasks/router.py:603-623`（retry 端点加 `force`）
- Test: `server/tests/test_recover_retry_guard.py`

**Interfaces:**
- Consumes: `commit_attempted_at`/`failure_kind`（Task 4）
- Produces: `recover_stuck_records` 对 `commit_attempted_at` 非空者标 `failed`+`commit_uncertain`（不回 pending）；`retry_record(db, record, force=False)`；retry 端点 `?force=true`

- [ ] **Step 1: 写失败测试**

```python
# server/tests/test_recover_retry_guard.py
from datetime import timedelta

import pytest

from server.app.shared.errors import ClientError
from server.tests.utils import build_test_app

pytestmark = pytest.mark.mysql


# ... 粘贴 Task 4 的 _seed_record helper ...


def test_recover_does_not_repend_commit_uncertain(monkeypatch):
    from server.app.core.time import utcnow
    from server.app.modules.tasks.models import PublishRecord
    from server.app.modules.tasks.service import recover_stuck_records

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            _task, rec = _seed_record(db, status="running")
            rec.lease_until = utcnow() - timedelta(minutes=5)
            rec.commit_attempted_at = utcnow() - timedelta(minutes=4)  # 已跨提交点
            db.commit()
            recover_stuck_records(db)
            db.refresh(rec)
            assert rec.status == "failed"
            assert rec.failure_kind == "commit_uncertain"  # 不回 pending
    finally:
        test_app.cleanup()


def test_retry_blocks_commit_uncertain_without_force(monkeypatch):
    from server.app.modules.tasks.service import retry_record

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            _task, rec = _seed_record(db, status="failed")
            rec.failure_kind = "commit_uncertain"
            db.commit()
            with pytest.raises(ClientError):
                retry_record(db, rec)  # force 默认 False → 拦截
            # force=True 放行（不抛）
            new_rec = retry_record(db, rec, force=True)
            assert new_rec.retry_of_record_id == rec.id
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 运行确认失败**

Run: `conda activate geo_xzpt && GEO_TEST_DATABASE_URL=...geo_test pytest server/tests/test_recover_retry_guard.py -q`
Expected: FAIL（recover 把 record 拨回 pending；retry_record 无 force 参）

- [ ] **Step 3a: `recover_stuck_records` 护栏** — `server/app/modules/tasks/service.py:376-389` 循环改为按 `commit_attempted_at` 分流：

```python
    for record in records:
        if record.commit_attempted_at is not None:
            # 死在提交中途：无法安全自动重跑，锁为「结果未知」等人工核对
            record.status = "failed"
            record.failure_kind = "commit_uncertain"
            record.finished_at = utcnow()
            record.lease_until = None
            db.add(
                TaskLog(
                    task_id=record.task_id,
                    record_id=record.id,
                    level="warn",
                    message="进程重启：记录已跨提交点，结果未知，请人工核对平台后再决定是否重发",
                )
            )
        else:
            record.status = "pending"
            record.lease_until = None
            db.add(
                TaskLog(
                    task_id=record.task_id,
                    record_id=record.id,
                    level="warn",
                    message="进程重启：记录在上次运行中意外中断，已重置为等待状态",
                )
            )
    if records:
        _logger.warning("Recovered %d stuck records: %s", len(records), [r.id for r in records])
        db.commit()
```

- [ ] **Step 3b: `retry_record` 加 force** — `service.py:296`：

```python
def retry_record(db: Session, record: PublishRecord, *, force: bool = False) -> PublishRecord:
    if record.status != "failed":
        raise ClientError(f"Only failed records can be retried: {record.status}")
    if record.retry_of_record_id is not None:
        raise ClientError(
            "Retry records cannot be retried again; create a new task after checking the platform result"
        )
    if record.failure_kind == "commit_uncertain" and not force:
        raise ClientError(
            "该记录已提交但结果未知，请先到平台核对是否已发布；确认未发布后再用强制重发（force=true）"
        )
    # ...（其余防重检查 + 建 retry 记录逻辑不变）...
```

- [ ] **Step 3c: 端点加 force** — `server/app/modules/tasks/router.py:603`：

```python
@publish_records_router.post("/{record_id}/retry", response_model=PublishRecordRead)
def retry_record_endpoint(
    record_id: int,
    request: Request,
    force: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PublishRecordRead:
    record = _verify_record_ownership(get_record(db, record_id), current_user, db)
    result = retry_record(db, record, force=force)
    db.commit()
    add_audit_entry(
        db,
        user=current_user,
        action="publish_record.retry",
        target_type="publish_record",
        target_id=record_id,
        payload={"new_record_id": result.id, "force": force},
        request=request,
    )
    _start_background_execute(record.task_id)
    return to_record_read(result)
```

- [ ] **Step 4: 运行确认通过**

Run: `conda activate geo_xzpt && GEO_TEST_DATABASE_URL=...geo_test pytest server/tests/test_recover_retry_guard.py -q`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/tasks/service.py server/app/modules/tasks/router.py server/tests/test_recover_retry_guard.py
git commit -m "feat(tasks): recover 不重跑 commit_uncertain + retry 默认拦截(force 旁路)"
```

---

## Task 10: 前端 — failure_kind 暴露 + 告警 + 禁用一键重试

**Files:**
- Modify: `server/app/modules/tasks/schemas.py:67-82`（`PublishRecordRead` 加 `failure_kind`）+ `:176-193`（`to_record_read` 映射）
- Modify: `web/src/types.ts:359`（`PublishRecord` type 加 `failure_kind`）+ `web/src/api/tasks.ts:54`（`retryRecord` 加可选 `{ force }`）
- Modify: `web/src/features/tasks/*`（发布记录行：`failure_kind==='commit_uncertain'` 时显示告警条 + 禁用「重试」按钮，提供「核对后强制重发」入口调 `?force=true`）
- Test: 后端 `server/tests/test_tasks_api.py` 追加断言 `failure_kind` 出现在记录响应；前端无单测框架 → `pnpm --filter @geo/web typecheck` + `build` 作门禁

**Interfaces:**
- Consumes: `failure_kind` 列（Task 4）、retry `?force`（Task 9）
- Produces: API 响应含 `failure_kind`；前端据此渲染

- [ ] **Step 1: 写失败测试（后端，追加到 test_tasks_api.py 合适用例）**

```python
def test_record_read_exposes_failure_kind(monkeypatch):
    # 在已有「失败记录」用例基础上断言序列化包含字段（默认 None 也应出现在 schema）
    from server.app.modules.tasks.schemas import PublishRecordRead

    assert "failure_kind" in PublishRecordRead.model_fields
```

- [ ] **Step 2: 运行确认失败**

Run: `conda activate geo_xzpt && pytest server/tests/test_tasks_api.py::test_record_read_exposes_failure_kind -q`
Expected: FAIL（schema 无 `failure_kind`）

- [ ] **Step 3a: schema** — `server/app/modules/tasks/schemas.py`，`PublishRecordRead` 在 `error_message` 后加：

```python
    failure_kind: str | None = None
```

`to_record_read` 构造里加：

```python
        failure_kind=getattr(record, "failure_kind", None),
```

- [ ] **Step 3b: 前端类型 + UI** — `web/src/types.ts:359` 的 `PublishRecord` type 加 `failure_kind?: string | null`（`tasks.ts` 仅 import 该 type，不在 `tasks.ts` 加字段）。发布记录 UI 在 `web/src/features/tasks/TasksWorkspace.tsx`（重试按钮约 `:676`）：

```tsx
{record.failure_kind === 'commit_uncertain' ? (
  <div className="record-warning">
    已提交但结果未知，请先到平台核对是否已发布。
    <button onClick={() => retryRecord(record.id, { force: true })}>核对后强制重发</button>
  </div>
) : (
  <button onClick={() => retryRecord(record.id)} disabled={record.status !== 'failed'}>重试</button>
)}
```

`web/src/api/tasks.ts` 的 `retryRecord` 加可选 `{ force }` → `POST /api/publish-records/${id}/retry?force=true`。

> 具体组件文件按 `web/src/features/tasks/` 现有发布记录渲染处定位（grep `publish-records` / `retry`）；保持既有样式类名风格。

- [ ] **Step 4: 运行确认通过**

Run:
```bash
conda activate geo_xzpt && pytest server/tests/test_tasks_api.py -q
pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build
```
Expected: 后端 PASS；前端 typecheck + build 绿（前端 CI 门禁）。

> 在 worktree 跑前端命令注意 cwd 漂移：用 `pnpm -C E:/geo/.claude/worktrees/typed-strolling-emerson/web ...` 或先 `cd` 到 worktree 的 web 目录确认 `pwd`（见记忆 gotcha-worktree-bash-cwd-drifts-to-main）。

- [ ] **Step 5: 提交**

```bash
git add server/app/modules/tasks/schemas.py web/src/api/tasks.ts web/src/features/tasks/ server/tests/test_tasks_api.py
git commit -m "feat(web): 发布记录暴露 failure_kind + commit_uncertain 告警/禁用一键重试"
```

---

## Final Verification

- [ ] 全量后端测试：`conda activate geo_xzpt && GEO_TEST_DATABASE_URL=...geo_test pytest server/tests/ -q`（含新增 + 零回归）
- [ ] Lint/format/type：`ruff check server/ && ruff format --check server/ && mypy server/app`
- [ ] 前端门禁：`pnpm --filter @geo/web typecheck && pnpm --filter @geo/web build`
- [ ] 手动确认 spec §7 硬约束：默认 `max_elapsed=60s` < `publish_record_timeout_seconds=300s` ✓

## Self-Review 记录（实现者勿删，供回看）

- **Spec 覆盖**：①resilience=Task1/2 ②CommitGuard/CommitUncertainError=Task3 ③标记列+迁移=Task4 ④API 驱动接入=Task5/6 ⑤浏览器驱动=Task5/7 ⑥executor 分流=Task8 ⑦记录层 recover/retry 护栏=Task9 ⑧配置=Task2 ⑨观测(on_retry 诊断)=见下「已知缩减」 ⑩前端=Task10。
- **已知缩减（YAGNI）**：spec §7 的 `on_retry`→`PublishDiagnosticEvent` 观测埋点未单列 Task。`retry_call` 已暴露 `on_retry` 钩子；接入诊断流（`capture_publish_diagnostics`）作为 Task6/7 内的可选增强（驱动调 `retry_call(on_retry=...)` 时塞一条 warn 日志即可），不阻塞主链路。若需独立验收，另起小 Task。`failure_kind` 仅用 `commit_uncertain`/`None`（spec 已注明列可容纳更多值）。
- **头条提交边界的范围裁剪（待需求方确认）**：Task 7 守卫**只包「确认发布」点击**，点击后的轮询检测（含「30s 未跳转→乐观判成功」）保持现状不动。即「点击成功后、轮询期网络中断」仍按既有行为判 succeeded（不引入回归）。若需求方要求轮询期断网也判 uncertain，按 Task 7「⚠️ 已知残留与待决」调整——代价是慢但成功的头条发布会变 `commit_uncertain`。**此决定在交付时单独抛给需求方**，默认保守不改。
- **迁移依赖**：Task 4 迁移须在并行加密 spec 的 accounts 迁移落 main 后再生成（`down_revision` 指向当时 head），见 Global Constraints。**实现期重新核对彼时真实 head**，勿照抄某个版本号——独立审查确认两 feature 当前 head 同为 `0048`，若各自独立 `down_revision=0048` 先后合入会瞬时双 head，靠「加密先落」约定串行化规避。
- **微信上传步重试的副作用（独立审查 m1）**：`upload_thumb` 走 `/cgi-bin/material/add_material?type=thumb`=**永久素材**（有配额）。`ReadTimeout` 后重传会产生重复永久素材、耗配额。**不威胁 at-most-once 发布正确性**（草稿仅由守卫保护的 `add_draft` 创建），仅资源卫生，且现状本就每发必传不去重，重试只是边际放大。正文图走 `media/uploadimg` 返回 URL、不占永久配额，近乎无副作用。spec §4「无副作用」宜读作「无发布副作用」。
- **per-step vs 聚合重试预算（独立审查 m2）**：Task 6 给每个上传步传同一个 `max_elapsed` 的 policy（per-step）。最坏 = `max_elapsed×(1+图片数)`，多图+持续抖动时可逼近单记录执行预算（默认 420s），watchdog 会先杀。后果可控（杀→recover 正确归类，非 at-most-once 违背）。如需收紧：可在 Task 6 按剩余图片数动态缩 `max_elapsed`，或给整个 `_publish_api` 包一个聚合时限。**本计划默认不收紧**（默认参数下 N 较小时不触发）。
- **API 驱动重试与 watchdog 交互（独立审查 m5）**：微信驱动也受 `_record_execution_budget()` watchdog 监控；退避 `time.sleep` 期间 watchdog 超时会触发超时处理（API 驱动无浏览器会话，`_close_record_browser` 为 no-op、`future.cancel()` 对运行中 future 无效，线程睡醒后自然结束）。既有架构如此、后果可控（终标 failed），列为覆盖说明而非缺陷。
- **类型一致性**：`commit_guard.committing()`、`retry_call(fn, *, policy, is_transient=...)`、`_mark_record_failed(..., failure_kind=...)`、`retry_record(db, record, *, force=False)` 全计划一致。
