"""Wave 0 / Task ACC —— 连接池负载复现签收脚本（一次性基线签收 + 稳态护栏，非持续门禁）。

度量「M 路并发排版、全部停在 LLM 调用那一刻时，*正被占用* 的连接数」——这是与池绝对容量
无关的纯隔离指标（测试 engine 默认池=15，生产=60），直接对应 Task 1 的论点「慢 IO 期间不持连接」。

用 LLM 内的 threading.Barrier(action=...) 在「M 路全部进入 LLM、且都还没离开」的瞬间采样
engine.pool.checkedout()——action 在最后一路到达、所有路仍阻塞时执行且仅执行一次，无竞态。

历史签收（Task 1a，见该 commit 与 docs/plans/2026-06-16-resource-hardening.md）：改造前旧单 session
路径整段持连接 → 采样 = M(=12)；三段式 → 0。该 before/after 对比依赖已被 Task 1b 删除的
_run_ai_format_single_session，故不再可跑，结论留在 git 历史与计划文档里。

Task 1b 后 **两条 fallback 路径都连接安全**：web_fallback=False 走三段式、web_fallback=True 走
五段式（决策→内存下载→短 session 落库），LLM 期间均不持连接。本脚本因此转为**稳态护栏**：在 M 路
并发下断言两种模式 LLM 期间被占用连接数均为 0。范围限定单进程：#110 的多进程放大由 Task 6/7 覆盖；
持续防回归仍靠 test_ai_format_connection_lifecycle.py 的确定性单测 + Task G 运行期断言。

opt-in：标 `load`，默认不跑；需 `GEO_RUN_LOAD_TESTS=1` + `GEO_TEST_DATABASE_URL`。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from server.tests.utils import build_test_app

_M = 12  # 并发排版数（< 测试 engine 默认池上限 15，给各段短 session 同时借连接留余量）


def _fake_completion(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _create_locked_articles(test_app, count: int) -> tuple[list[int], datetime]:
    from server.app.modules.articles.models import Article

    lock_started_at = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    ids: list[int] = []
    for i in range(count):
        resp = test_app.client.post(
            "/api/articles",
            json={
                "title": f"load-{i}",
                "content_json": {
                    "type": "doc",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": f"正文段落 {i}"}],
                        }
                    ],
                },
            },
        )
        assert resp.status_code == 200
        ids.append(resp.json()["id"])
    with test_app.session_factory() as db:
        for aid in ids:
            article = db.get(Article, aid)
            article.ai_checking = True
            article.ai_checking_started_at = lock_started_at
        db.commit()
    return ids, lock_started_at


def _peak_checkout_during_llm(test_app, monkeypatch, *, web_fallback: bool) -> int:
    """跑 _M 路并发 run_ai_format，返回「全部停在 LLM 内那一刻」被占用的连接数。"""
    from server.app.modules.articles import ai_format

    ids, lock_started_at = _create_locked_articles(test_app, _M)

    sampled: dict[str, int] = {}

    def _sample_when_all_in_llm():
        # barrier action：最后一路到达、所有路仍阻塞在 LLM 内时执行一次
        sampled["checked_out"] = test_app.engine.pool.checkedout()

    llm_barrier = threading.Barrier(_M, action=_sample_when_all_in_llm, timeout=20)

    def _fake_llm(**_):
        llm_barrier.wait()
        return _fake_completion('{"heading_indices": []}')

    monkeypatch.setattr(ai_format, "_call_litellm_completion", _fake_llm)

    def _run(aid: int) -> None:
        ai_format.run_ai_format(
            aid, include_images=False, web_fallback=web_fallback, lock_started_at=lock_started_at
        )

    with ThreadPoolExecutor(max_workers=_M) as ex:
        list(ex.map(_run, ids))

    assert "checked_out" in sampled, "barrier action 未触发：可能有路径在 LLM 前就失败"
    return sampled["checked_out"]


@pytest.mark.mysql
@pytest.mark.load
def test_no_connection_held_during_llm_either_fallback_mode(monkeypatch):
    """稳态护栏（Task 1a + 1b）：M 路并发下，两种 fallback 模式 LLM 期间均不持连接。"""
    monkeypatch.setenv("GEO_AI_FORMAT_API_KEY", "test-key")
    test_app = build_test_app(monkeypatch)
    try:
        web_fallback_peak = _peak_checkout_during_llm(test_app, monkeypatch, web_fallback=True)
        structured_peak = _peak_checkout_during_llm(test_app, monkeypatch, web_fallback=False)

        print(
            f"\n[Task ACC] connections held DURING LLM (M={_M}): "
            f"web_fallback=True(5-seg)={web_fallback_peak}  "
            f"web_fallback=False(3-seg)={structured_peak}"
        )

        # 三段式（web_fallback=False，Task 1a）：LLM 期间一条都不持
        assert structured_peak == 0, (
            f"web_fallback=False must hold 0 connections during the LLM call, got {structured_peak}"
        )
        # 五段式（web_fallback=True，Task 1b）：LLM 期间同样不持
        assert web_fallback_peak == 0, (
            f"web_fallback=True must hold 0 connections during the LLM call, got {web_fallback_peak}"
        )
    finally:
        test_app.cleanup()
