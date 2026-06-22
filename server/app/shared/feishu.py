"""飞书 Webhook 通知（发布任务完成播报）。

走 GEO_FEISHU_WEBHOOK_URL，与问题库同步用的自建应用凭据（APP_ID/APP_SECRET，
见 feishu_bitable）是两条不同凭据。未配置 Webhook 时静默跳过。
"""

import json
import logging
import threading
import urllib.request

from server.app.core.config import get_settings

_logger = logging.getLogger(__name__)


def notify_task_finished(
    task_name: str,
    task_id: int,
    status: str,
    total: int,
    succeeded: int,
    failed: int,
) -> None:
    """异步发送飞书通知（发出即不等待，不阻塞调用方）。"""
    url = get_settings().feishu_webhook_url
    if not url:
        return
    threading.Thread(
        target=_send,
        args=(url, task_name, task_id, status, total, succeeded, failed),
        daemon=True,
    ).start()


def _send(url, task_name, task_id, status, total, succeeded, failed) -> None:
    status_emoji = {
        "succeeded": "✅",
        "partial_failed": "⚠️",
        "failed": "❌",
        "cancelled": "🚫",
    }.get(status, "📋")
    text = (
        f"【geo】{status_emoji} 发布任务完成\n"
        f"任务：{task_name}（#{task_id}）\n"
        f"状态：{status}\n"
        f"结果：成功 {succeeded} / 失败 {failed} / 共 {total}"
    )
    payload = json.dumps({"msg_type": "text", "content": {"text": text}}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            _logger.info("Feishu notification sent for task %d: %s", task_id, body[:200])
    except Exception:
        _logger.warning("Failed to send Feishu notification for task %d", task_id, exc_info=True)


def send_text(title: str, message: str, level: str = "info") -> bool:
    """通用飞书通知（同步发送，立即返回是否成功）。

    level ∈ info / warning / error / done — 用于决定 emoji 前缀，不影响 webhook 路由。
    return True 表示已发出（不保证对方收到），False 表示 webhook 未配置或网络失败。
    """
    url = get_settings().feishu_webhook_url
    if not url:
        return False
    emoji = {"info": "💬", "warning": "⚠️", "error": "❌", "done": "✅"}.get(level, "💬")
    text = f"【geo】{emoji} {title}\n{message}"
    payload = json.dumps({"msg_type": "text", "content": {"text": text}}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception:
        _logger.warning("Failed to send Feishu notification", exc_info=True)
        return False
