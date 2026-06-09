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
