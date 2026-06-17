"""Task 5（封堵 #4）：启动期连接预算断言 —— 纯逻辑契约。

保守上界：`anyio_pool_size + publish_max + safety_margin ≤ pool_size + max_overflow`。
- 每个 anyio 同步端点线程经 get_db 至多持 1 个 DB 连接，故 anyio 池大小是 web 进程
  稳态并发持连接数的**上界**；默认 `40 + 5 + 10 = 55 ≤ 60` 通过。
- 越界时经 resource_metrics 的统一告警 hook（emit_resource_alert）告警，而不是孤立 WARNING；
  杠杆是扩池 / 降 publish_max，**绝不缩 anyio**。

这些用例不依赖 DB（纯函数 + monkeypatch 池快照），不带 @pytest.mark.mysql。
"""

from __future__ import annotations

from server.app.shared import resource_metrics as rm


# ── 纯函数 compute_connection_budget ──────────────────────────────────────────
def test_compute_within_budget_default():
    """默认 40 + 5 + 10 = 55 ≤ 60：within_budget True，算式明细如实回填。"""
    b = rm.compute_connection_budget(
        anyio_pool_size=40, publish_max=5, safety_margin=10, pool_capacity=60
    )
    assert b["needed"] == 55
    assert b["pool_capacity"] == 60
    assert b["within_budget"] is True
    assert b["anyio_pool_size"] == 40
    assert b["publish_max"] == 5
    assert b["safety_margin"] == 10


def test_compute_over_budget():
    """容量 50 < 需求 55：within_budget False。"""
    b = rm.compute_connection_budget(
        anyio_pool_size=40, publish_max=5, safety_margin=10, pool_capacity=50
    )
    assert b["needed"] == 55
    assert b["within_budget"] is False


def test_compute_boundary_equal_is_within():
    """需求恰等于容量（55 == 55）：≤ 故仍算 within（边界不误报）。"""
    b = rm.compute_connection_budget(
        anyio_pool_size=40, publish_max=5, safety_margin=10, pool_capacity=55
    )
    assert b["needed"] == 55
    assert b["within_budget"] is True


# ── 启动期 check_connection_budget 走告警 hook ─────────────────────────────────
def _capture_alerts(monkeypatch) -> list[tuple[str, dict | None]]:
    """把全局告警 hook 换成收集器，返回收集列表（自动随 monkeypatch 还原）。"""
    captured: list[tuple[str, dict | None]] = []
    monkeypatch.setattr(rm, "_alert_hook", lambda msg, ctx=None: captured.append((msg, ctx)))
    return captured


def test_check_emits_alert_when_over_budget(monkeypatch):
    """池容量被压到极小（1）：无论 env 里 publish_max/margin 取何值，必越界 → 告警 hook 触发一次。"""
    monkeypatch.setattr(rm, "_collect_pool", lambda: {"max": 1})
    alerts = _capture_alerts(monkeypatch)

    budget = rm.check_connection_budget()

    assert budget["within_budget"] is False
    assert len(alerts) == 1
    msg = alerts[0][0]
    assert "budget" in msg.lower()  # 文案点明"连接预算越界"


def test_check_silent_when_within_budget(monkeypatch):
    """池容量极大（100000）：必在预算内 → 不告警。"""
    monkeypatch.setattr(rm, "_collect_pool", lambda: {"max": 100000})
    alerts = _capture_alerts(monkeypatch)

    budget = rm.check_connection_budget()

    assert budget["within_budget"] is True
    assert alerts == []


def test_check_silent_when_pool_unavailable(monkeypatch):
    """池状态不可用（max=-1）：不应误报告警（容量未知 ≠ 越界）。"""
    monkeypatch.setattr(rm, "_collect_pool", lambda: {"max": -1})
    alerts = _capture_alerts(monkeypatch)

    rm.check_connection_budget()

    assert alerts == []


def test_check_never_raises(monkeypatch):
    """_collect_pool 抛错也不能让启动期检查崩溃（绝不阻塞 create_app）。"""

    def _boom():
        raise RuntimeError("pool blew up")

    monkeypatch.setattr(rm, "_collect_pool", _boom)
    # 不抛即通过
    rm.check_connection_budget()
