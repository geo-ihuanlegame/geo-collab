"""
Phase 5 测试：飞书通知功能

场景：
1. notify_task_finished 在 webhook URL 为 None 时不报错、不发送
2. _send 发送正确的 JSON 载荷（monkeypatch urllib.request.urlopen）
3. 发送失败（urlopen 抛异常）时不抛出（静默记录 warning）
4. 任务完成后飞书通知被触发（monkeypatch notify_task_finished，
   在 _aggregate_task_status 调用结束后可观察到）
"""

import json
import time
from io import BytesIO
from unittest.mock import patch

from server.app.core.config import get_settings
from server.app.shared.feishu import _send, notify_task_finished
from server.tests.utils import build_test_app

# ── 辅助函数 ────────────────────────────────────────────────────────────────

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _create_article(client, title="Test") -> int:
    cover = client.post(
        "/api/assets", files={"file": ("c.png", BytesIO(_PNG), "image/png")}
    ).json()["id"]
    return client.post(
        "/api/articles",
        json={
            "title": title,
            "content_json": {"type": "doc", "content": []},
            "plain_text": "body",
            "cover_asset_id": cover,
        },
    ).json()["id"]


def _create_account(test_app, key="acc-feishu") -> int:
    d = test_app.data_dir / "browser_states" / "toutiao" / key
    d.mkdir(parents=True, exist_ok=True)
    (d / "storage_state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    return test_app.client.post(
        "/api/accounts/toutiao/login",
        json={"display_name": "Acc Feishu", "account_key": key, "use_browser": False},
    ).json()["id"]


def _create_task(client, article_id: int, account_id: int, name: str = "Feishu task") -> dict:
    return client.post(
        "/api/tasks",
        json={
            "name": name,
            "task_type": "single",
            "article_id": article_id,
            "accounts": [{"account_id": account_id}],
        },
    ).json()


# ── 场景 1: webhook URL 为 None 时不报错、不发送 ──────────────────────────────


class TestNotifyNoWebhook:
    def test_returns_immediately_without_sending(self, monkeypatch):
        """当 feishu_webhook_url 为 None 时，notify_task_finished 直接返回，不启动线程。"""
        monkeypatch.setenv("GEO_FEISHU_WEBHOOK_URL", "")
        get_settings.cache_clear()

        sent = []

        def fake_thread_start(self):
            sent.append(True)

        with patch("threading.Thread.start", fake_thread_start):
            notify_task_finished(
                task_name="test",
                task_id=1,
                status="succeeded",
                total=1,
                succeeded=1,
                failed=0,
            )

        assert not sent, "No thread should be started when webhook URL is not set"
        get_settings.cache_clear()

    def test_does_not_raise_when_url_is_none(self, monkeypatch):
        """即使 webhook url 为 None，调用不应抛出任何异常。"""
        # 确保环境中没有 webhook URL
        monkeypatch.delenv("GEO_FEISHU_WEBHOOK_URL", raising=False)
        get_settings.cache_clear()

        # 应无异常
        notify_task_finished(
            task_name="no-webhook",
            task_id=99,
            status="failed",
            total=2,
            succeeded=0,
            failed=2,
        )
        get_settings.cache_clear()


# ── 场景 2: _send 发送正确的 JSON 载荷 ───────────────────────────────────────


class TestSendPayload:
    def test_sends_correct_json(self):
        """_send 应发送包含正确字段的 JSON payload 到 webhook URL。"""
        captured_requests = []

        class FakeResponse:
            def read(self):
                return b'{"StatusCode":0}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def fake_urlopen(req, timeout=None):
            captured_requests.append(req)
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            _send(
                url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
                task_name="我的任务",
                task_id=42,
                status="succeeded",
                total=3,
                succeeded=3,
                failed=0,
            )

        assert len(captured_requests) == 1
        req = captured_requests[0]
        assert req.full_url == "https://open.feishu.cn/open-apis/bot/v2/hook/test"
        assert req.headers.get("Content-type") == "application/json"

        body = json.loads(req.data.decode())
        assert body["msg_type"] == "text"
        text = body["content"]["text"]
        assert "我的任务" in text
        assert "#42" in text
        assert "succeeded" in text
        assert "3" in text  # 总数

    def test_sends_correct_emoji_for_each_status(self):
        """_send 应为不同状态发送对应的 emoji。"""
        status_emoji_map = {
            "succeeded": "✅",
            "partial_failed": "⚠️",
            "failed": "❌",
            "cancelled": "🚫",
        }
        for status, expected_emoji in status_emoji_map.items():
            captured = []

            class FakeResp:
                def read(self):
                    return b"{}"

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

            def fake_urlopen(req, timeout=None, captured=captured):
                captured.append(req)
                return FakeResp()

            with patch("urllib.request.urlopen", fake_urlopen):
                _send("http://example.com", "task", 1, status, 1, 0, 0)

            body = json.loads(captured[0].data.decode())
            assert expected_emoji in body["content"]["text"], (
                f"Expected {expected_emoji} for status {status}"
            )

    def test_payload_contains_failed_count(self):
        """_send 的 payload 中应包含失败数量。"""
        captured = []

        class FakeResp:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def fake_urlopen(req, timeout=None):
            captured.append(req)
            return FakeResp()

        with patch("urllib.request.urlopen", fake_urlopen):
            _send("http://example.com", "test task", 7, "partial_failed", 5, 3, 2)

        body = json.loads(captured[0].data.decode())
        text = body["content"]["text"]
        assert "2" in text  # 失败数
        assert "3" in text  # 成功数
        assert "5" in text  # 总数


# ── 场景 3: 发送失败时静默记录 warning，不抛出 ────────────────────────────────


class TestSendFailureSilent:
    def test_does_not_raise_on_network_error(self):
        """urlopen 抛出异常时，_send 应静默处理，不向上抛出。"""

        def fake_urlopen(req, timeout=None):
            raise OSError("Connection refused")

        # 不应抛出
        with patch("urllib.request.urlopen", fake_urlopen):
            _send("http://example.com", "task", 1, "failed", 1, 0, 1)

    def test_does_not_raise_on_http_error(self):
        """HTTP 错误（4xx/5xx）时，_send 应静默处理，不向上抛出。"""
        import urllib.error

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                url="http://example.com",
                code=500,
                msg="Internal Server Error",
                hdrs=None,
                fp=None,
            )

        with patch("urllib.request.urlopen", fake_urlopen):
            _send("http://example.com", "task", 2, "partial_failed", 2, 1, 1)

    def test_logs_warning_on_failure(self, caplog):
        """urlopen 失败时应写入 warning 日志。"""
        import logging

        def fake_urlopen(req, timeout=None):
            raise OSError("timeout")

        with patch("urllib.request.urlopen", fake_urlopen):
            with caplog.at_level(logging.WARNING, logger="server.app.shared.feishu"):
                _send("http://example.com", "mytask", 3, "failed", 1, 0, 1)

        assert any("3" in r.message or "Feishu" in r.message for r in caplog.records), (
            f"Expected warning log, got: {[r.message for r in caplog.records]}"
        )


# ── 场景 4: 任务完成后飞书通知被触发 ──────────────────────────────────────────


class TestNotifyTriggeredOnTaskCompletion:
    def test_notify_called_when_task_succeeds(self, monkeypatch):
        """任务所有 record 成功后，notify_task_finished 应被调用一次。"""
        test_app = build_test_app(monkeypatch)
        monkeypatch.setenv("GEO_FEISHU_WEBHOOK_URL", "http://fake-feishu-hook.test/")
        get_settings.cache_clear()

        notify_calls = []

        def fake_notify(task_name, task_id, status, total, succeeded, failed):
            notify_calls.append(
                {
                    "task_name": task_name,
                    "task_id": task_id,
                    "status": status,
                    "total": total,
                    "succeeded": succeeded,
                    "failed": failed,
                }
            )

        monkeypatch.setattr("server.app.shared.feishu.notify_task_finished", fake_notify)

        try:
            article_id = _create_article(test_app.client)
            account_id = _create_account(test_app)
            task_data = _create_task(test_app.client, article_id, account_id)
            task_id = task_data["id"]

            class FakeResult:
                url = "https://toutiao.com/article/123"
                message = "Published"

            monkeypatch.setattr(
                "server.app.modules.tasks.executor.build_publish_runner_for_record",
                lambda _r: lambda article, account, *, stop_before_publish=False: FakeResult(),
            )

            test_app.client.post(f"/api/tasks/{task_id}/execute")

            # 等待后台线程结束
            deadline = time.time() + 5.0
            while time.time() < deadline:
                task = test_app.client.get(f"/api/tasks/{task_id}").json()
                if task["status"] in ("succeeded", "failed", "partial_failed"):
                    break
                time.sleep(0.05)

            assert notify_calls, "notify_task_finished should have been called"
            call = notify_calls[0]
            assert call["task_id"] == task_id
            assert call["status"] == "succeeded"
            assert call["total"] == 1
            assert call["succeeded"] == 1
            assert call["failed"] == 0
        finally:
            get_settings.cache_clear()
            test_app.cleanup()

    def test_notify_called_when_task_fails(self, monkeypatch):
        """任务所有 record 失败后，notify_task_finished 应以 'failed' 状态被调用。"""
        test_app = build_test_app(monkeypatch)
        monkeypatch.setenv("GEO_FEISHU_WEBHOOK_URL", "http://fake-feishu-hook.test/")
        get_settings.cache_clear()

        notify_calls = []

        def fake_notify(task_name, task_id, status, total, succeeded, failed):
            notify_calls.append(
                {
                    "status": status,
                    "total": total,
                    "succeeded": succeeded,
                    "failed": failed,
                }
            )

        monkeypatch.setattr("server.app.shared.feishu.notify_task_finished", fake_notify)

        try:
            article_id = _create_article(test_app.client)
            account_id = _create_account(test_app, key="acc-feishu-fail")
            task_data = _create_task(test_app.client, article_id, account_id, name="Fail task")
            task_id = task_data["id"]

            from server.app.modules.tasks.drivers.toutiao import ToutiaoPublishError

            monkeypatch.setattr(
                "server.app.modules.tasks.executor.build_publish_runner_for_record",
                lambda _r: (
                    lambda article, account, *, stop_before_publish=False: (_ for _ in ()).throw(
                        ToutiaoPublishError("publish failed", screenshot=None)
                    )
                ),
            )

            test_app.client.post(f"/api/tasks/{task_id}/execute")

            deadline = time.time() + 5.0
            while time.time() < deadline:
                task = test_app.client.get(f"/api/tasks/{task_id}").json()
                if task["status"] in ("succeeded", "failed", "partial_failed"):
                    break
                time.sleep(0.05)

            assert notify_calls, "notify_task_finished should have been called"
            call = notify_calls[0]
            assert call["status"] == "failed"
            assert call["total"] == 1
            assert call["failed"] == 1
        finally:
            get_settings.cache_clear()
            test_app.cleanup()

    def test_notify_not_called_when_url_not_set(self, monkeypatch):
        """当 webhook URL 未配置时，即使任务完成，也不应触发网络请求。"""
        test_app = build_test_app(monkeypatch)
        # setenv 覆盖 .env 文件中的同名配置；delenv 只删除进程环境变量，
        # 无法屏蔽 pydantic-settings 从 .env 文件读到的值
        monkeypatch.setenv("GEO_FEISHU_WEBHOOK_URL", "")
        get_settings.cache_clear()

        urlopen_calls = []

        def fake_urlopen(req, timeout=None):
            urlopen_calls.append(req)
            raise AssertionError("urlopen should not be called when URL is not set")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        try:
            article_id = _create_article(test_app.client)
            account_id = _create_account(test_app, key="acc-feishu-nourl")
            task_data = _create_task(test_app.client, article_id, account_id, name="No URL task")
            task_id = task_data["id"]

            class FakeResult:
                url = "https://toutiao.com/article/456"
                message = "Published"

            monkeypatch.setattr(
                "server.app.modules.tasks.executor.build_publish_runner_for_record",
                lambda _r: lambda article, account, *, stop_before_publish=False: FakeResult(),
            )

            test_app.client.post(f"/api/tasks/{task_id}/execute")

            deadline = time.time() + 5.0
            while time.time() < deadline:
                task = test_app.client.get(f"/api/tasks/{task_id}").json()
                if task["status"] in ("succeeded", "failed", "partial_failed"):
                    break
                time.sleep(0.05)

            # 给后台线程一点时间，观察是否会调用 urlopen
            time.sleep(0.2)
            assert not urlopen_calls, "urlopen should not have been called"
        finally:
            get_settings.cache_clear()
            test_app.cleanup()
