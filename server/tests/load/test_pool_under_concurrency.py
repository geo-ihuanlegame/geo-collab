"""Wave 0 / Task ACC —— 连接池负载复现签收脚本（一次性基线签收，非持续门禁）。

度量「M 路并发排版、全部停在 LLM 调用那一刻时，*正被占用* 的连接数」——这是与池绝对容量
无关的纯隔离指标（测试 engine 默认池=15，生产=60），直接对应 Task 1a 的论点「慢 IO 期间不持连接」。

用 LLM 内的 threading.Barrier(action=...) 在「M 路全部进入 LLM、且都还没离开」的瞬间采样
engine.pool.checkedout()——action 在最后一路到达、所有路仍阻塞时执行且仅执行一次，无竞态：
- before：web_fallback=True 路由到 _run_ai_format_single_session（旧单 session，行为同改造前），
  整段持连接 → 采样 = M。
- after ：web_fallback=False 三段式，段1 已 close、段3 未起，LLM 期间 → 采样 = 0。

两路都用 *当前代码* 实测（旧路径保留在 _run_ai_format_single_session 里），所以这份对比可重复跑、
不依赖 git stash。范围限定单进程：#110 的多进程放大由 Task 6/7 覆盖。持续防回归不靠本脚本，
靠 test_ai_format_connection_lifecycle.py 的确定性单测 + Task G 运行期断言。

opt-in：标 `load`，默认不跑；需 `GEO_RUN_LOAD_TESTS=1` + `GEO_TEST_DATABASE_URL`。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from server.tests.utils import build_test_app

_M = 12  # 并发排版数（< 测试 engine 默认池上限 15，避免 before 路径在 LLM 处因池满互相死锁）


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
def test_connection_holding_during_llm_before_vs_after(monkeypatch):
    monkeypatch.setenv("GEO_AI_FORMAT_API_KEY", "test-key")
    test_app = build_test_app(monkeypatch)
    try:
        before = _peak_checkout_during_llm(test_app, monkeypatch, web_fallback=True)
        after = _peak_checkout_during_llm(test_app, monkeypatch, web_fallback=False)

        print(
            f"\n[Task ACC] connections held DURING LLM (M={_M}): "
            f"before(single-session)={before}  after(3-seg)={after}"
        )

        # 改造前（旧单 session 路径）：整段持连接 → M 条全被钉在 LLM 期间
        assert before == _M, f"before proxy should pin {_M} connections during LLM, got {before}"
        # 改造后（Task 1a 三段式）：LLM 期间一条都不持
        assert after == 0, f"Task 1a must hold 0 connections during the LLM call, got {after}"
    finally:
        test_app.cleanup()
