"""core/logging 的运行上下文：RunContextFilter 注入 + submit_in_context 跨线程透传（无需 DB）。"""

import logging
from concurrent.futures import ThreadPoolExecutor

from server.app.core.logging import (
    RunContextFilter,
    bind_node,
    bind_run,
    clear_run_context,
    submit_in_context,
)


def _runctx_of() -> str:
    """构造一条 LogRecord，过滤器跑一遍，返回注入的 runctx 串。"""
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
    RunContextFilter().filter(rec)
    return rec.runctx


def test_filter_empty_when_unbound():
    clear_run_context()
    assert _runctx_of() == ""


def test_filter_injects_run_pipe_node():
    try:
        bind_run(123, 7)
        bind_node(2, "ai_compose")
        assert _runctx_of() == "[run=123 pipe=7 node=2:ai_compose] "
    finally:
        clear_run_context()


def test_clear_resets():
    bind_run(1, 1)
    clear_run_context()
    assert _runctx_of() == ""


def test_submit_in_context_propagates_to_worker_thread():
    """contextvars 不跨线程自动继承；submit_in_context 应把当前上下文带进子线程。"""
    try:
        bind_run(55, 9)
        bind_node(3, "ai_generate")

        def _job() -> str:
            return _runctx_of()  # 在子线程里读上下文

        with ThreadPoolExecutor(max_workers=2) as pool:
            # 裸 submit 拿不到上下文（对照）
            plain = pool.submit(_job).result()
            # submit_in_context 应带上
            wrapped = submit_in_context(pool, _job).result()

        assert wrapped == "[run=55 pipe=9 node=3:ai_generate] "
        assert plain == ""  # 对照：未透传则子线程无上下文
    finally:
        clear_run_context()
