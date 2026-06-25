"""MinIO 客户端超时守卫（演示前加固）。

上传图片走 async 路由（image_library/router.py:upload_image），其内部的 minio
`put_object` 是同步阻塞调用。minio 7.x 默认 http client 是
`Timeout(connect=300, read=300)` + `Retry(total=5)`——MinIO 一旦假死，单次调用会
把整个事件循环冻最多 ~5 分钟。本测试钉死 `_client()` 必须用有界短超时覆盖该默认值。

无需 DB、无需网络：只构造 minio 客户端并检查其内部 urllib3 PoolManager 的超时配置。
"""

from __future__ import annotations

from server.app.core.config import get_settings


def test_client_uses_bounded_http_timeout(monkeypatch):
    monkeypatch.setenv("GEO_MINIO_ENDPOINT", "minio.invalid:9000")
    monkeypatch.setenv("GEO_MINIO_ACCESS_KEY", "k")
    monkeypatch.setenv("GEO_MINIO_SECRET_KEY", "s")
    monkeypatch.setenv("GEO_MINIO_SECURE", "false")
    get_settings.cache_clear()
    try:
        from server.app.modules.image_library import store

        client = store._client()
        pool_kw = client._http.connection_pool_kw

        # minio 默认是 Timeout(connect=300, read=300)；必须被我们的短超时覆盖。
        timeout = pool_kw["timeout"]
        assert timeout.connect_timeout == 5
        assert timeout.read_timeout == 30

        # 重试要有界；read=False 避免 20MB PUT body 在读超时时被整体重发。
        retries = pool_kw["retries"]
        assert retries.total == 1
        assert retries.read is False
    finally:
        get_settings.cache_clear()
