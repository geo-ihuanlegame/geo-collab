"""Task G —— 运行期长持连接护栏：连接 checkout 超阈值才归还即告警（防 A 类复发，#1/#110 同源）。

纯逻辑 / 事件 mock，不需要 DB：用假的 connection_record（带 .info dict）+ 注入的 clock / alert
驱动 checkout / checkin 处理器，验证「持有 > 阈值 → 告警含时长 + 阈值 + 线程线索」「短借不告警」
「未配对的 checkin 安全」。运行期长持靠这层自动捕获，不靠人自觉看 CLAUDE.md。
"""

from __future__ import annotations

from types import SimpleNamespace

from server.app.shared.connection_watchdog import ConnectionWatchdog


class _Clock:
    """可手动推进的假时钟。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _make_record() -> SimpleNamespace:
    # 模仿 SQLAlchemy ConnectionRecord：跨 checkout/checkin 持久的 .info dict。
    return SimpleNamespace(info={})


def _spy() -> tuple[list[tuple[str, dict]], object]:
    captured: list[tuple[str, dict]] = []

    def alert(message: str, context: dict | None = None) -> None:
        captured.append((message, context or {}))

    return captured, alert


def test_long_hold_triggers_alert():
    clock = _Clock()
    alerts, alert = _spy()
    wd = ConnectionWatchdog(threshold_seconds=30.0, clock=clock, alert=alert)

    rec = _make_record()
    wd.on_checkout(None, rec, None)  # t=0 借出
    clock.t = 31.0
    wd.on_checkin(None, rec)  # 持有 31s > 30s 阈值

    assert len(alerts) == 1
    message, context = alerts[0]
    assert "31" in message  # 持有时长
    assert "30" in message  # 阈值
    assert context.get("held_seconds") is not None and context["held_seconds"] >= 30
    assert "thread" in context  # 线程线索（配合 Task 3 的 thread_name_prefix 定位调用方）


def test_short_hold_no_alert():
    clock = _Clock()
    alerts, alert = _spy()
    wd = ConnectionWatchdog(threshold_seconds=30.0, clock=clock, alert=alert)

    rec = _make_record()
    wd.on_checkout(None, rec, None)  # t=0
    clock.t = 5.0
    wd.on_checkin(None, rec)  # 持有 5s，正常短借

    assert alerts == []


def test_checkin_without_checkout_is_safe():
    alerts, alert = _spy()
    wd = ConnectionWatchdog(threshold_seconds=30.0, clock=_Clock(), alert=alert)

    # 没有先 checkout（如护栏注册前就借出的连接归还）：不得抛错、不得误报。
    wd.on_checkin(None, _make_record())

    assert alerts == []
