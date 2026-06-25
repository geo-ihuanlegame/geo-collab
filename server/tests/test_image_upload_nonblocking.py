"""Part B 守卫（演示前加固）：图片上传 async 路由必须把阻塞的 minio 调用丢线程池，
不能 inline 跑在事件循环上——否则一次慢 / 假死的 MinIO 上传会冻住整个 web 进程。

并发发 3 个上传，stub minio 上传阻塞 0.6s：
  - inline 在事件循环上（未修）→ 串行 ≈ 1.8s
  - run_in_executor 丢线程池（已修）→ 重叠 ≈ 0.6s

需要 MySQL（build_test_app）。
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from server.tests.utils import build_test_app

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128


@pytest.mark.mysql
def test_upload_does_not_block_event_loop(monkeypatch):
    app_ctx = build_test_app(monkeypatch)
    try:
        client = app_ctx.client
        # 测试环境无 MinIO：建桶 no-op；上传 stub 成阻塞 0.6s（模拟慢 / 假死的 put_object）。
        monkeypatch.setattr(
            "server.app.modules.image_library.router.minio_store.ensure_bucket",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            "server.app.modules.image_library.router.minio_store.upload_image",
            lambda *a, **k: time.sleep(0.6),
        )

        r = client.post(
            "/api/image-library/categories",
            json={"name": "demo", "bucket_name": "demo-bucket"},
        )
        assert r.status_code == 201, r.text
        category_id = r.json()["id"]

        asgi_app = client.app
        token = client.cookies.get("access_token")

        async def fire_three():
            transport = httpx.ASGITransport(app=asgi_app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                cookies={"access_token": token},
            ) as ac:

                async def one():
                    return await ac.post(
                        f"/api/image-library/images?category_id={category_id}",
                        files={"file": ("a.png", _PNG, "image/png")},
                    )

                t0 = time.monotonic()
                responses = await asyncio.gather(one(), one(), one())
                return time.monotonic() - t0, responses

        elapsed, responses = asyncio.run(fire_three())
        for resp in responses:
            assert resp.status_code == 201, resp.text
        # 3×0.6s：串行 ≈ 1.8s（被事件循环阻塞）vs 重叠 ≈ 0.6s（已丢线程池）。
        assert elapsed < 1.2, (
            f"并发上传被串行化（{elapsed:.2f}s）——阻塞调用仍在事件循环上，Part B 未生效"
        )
    finally:
        app_ctx.cleanup()
