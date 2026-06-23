"""通用退避重试：平台无关、零 ORM。供发布链路（及未来 hot_lists/feishu/litellm）复用。

retry_call 只对 is_transient(exc)==True 的异常退避重试；其余立即抛。
不在此判定「提交边界是否安全」——那是 drivers.base.CommitGuard 的职责。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass


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


def retry_call[T](
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
            if (
                policy.max_elapsed is not None
                and (monotonic() - start) + delay > policy.max_elapsed
            ):
                raise
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleeper(delay)
