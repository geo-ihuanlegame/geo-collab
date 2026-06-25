"""图片库 MinIO 对象存储封装：分桶 / 对象的增删读，被路由与配图链路复用。

每次调用都新建 Minio 客户端（无连接池），凭据从 settings 读取。
"""

from __future__ import annotations

import io
import json
import logging

_logger = logging.getLogger(__name__)


def _client():
    # 懒导入 minio 与 settings：避免模块导入期就拉起依赖 / 读配置
    import os

    import certifi
    from minio import Minio
    from urllib3 import PoolManager, Retry
    from urllib3.util import Timeout

    from server.app.core.config import get_settings

    s = get_settings()
    # minio 默认 http client 是 Timeout(connect=300, read=300) + Retry(total=5)（见
    # minio/api.py）——MinIO 一旦假死，单次同步调用会把调用方阻塞最多 ~5 分钟；而上传
    # 走 async 路由，会直接冻住整个事件循环。这里传入显式短超时的 PoolManager 覆盖默认值。
    # maxsize / cert_reqs / ca_certs 照抄 minio 默认值，避免 HTTPS（secure=True）部署的 TLS 回归；
    # retries 用 read=False 避免读超时把 20MB PUT body 整体重发。
    http_client = PoolManager(
        timeout=Timeout(connect=5, read=30),
        maxsize=10,
        cert_reqs="CERT_REQUIRED",
        ca_certs=os.environ.get("SSL_CERT_FILE") or certifi.where(),
        retries=Retry(
            total=1, read=False, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504]
        ),
    )
    return Minio(
        s.minio_endpoint,
        access_key=s.minio_access_key,
        secret_key=s.minio_secret_key,
        secure=s.minio_secure,
        http_client=http_client,
    )


def ensure_bucket(bucket_name: str) -> None:
    """创建分桶（幂等），设置公开读策略。"""
    client = _client()
    if not client.bucket_exists(bucket_name):
        client.make_bucket(bucket_name)
    # 设置分桶公开读策略（s3:GetObject 放行）；注意嵌入文章的图片实际走 /api/stock-images/{id}/file 代理读取，并不依赖此策略
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": ["*"]},
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{bucket_name}/*"],
            }
        ],
    }
    client.set_bucket_policy(bucket_name, json.dumps(policy))
    _logger.info("ensured bucket: %s", bucket_name)


def upload_image(bucket_name: str, key: str, data: bytes, content_type: str) -> None:
    client = _client()
    client.put_object(
        bucket_name,
        key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )


def get_object_bytes(bucket_name: str, key: str) -> bytes:
    client = _client()
    response = client.get_object(bucket_name, key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def delete_object(bucket_name: str, key: str) -> None:
    client = _client()
    client.remove_object(bucket_name, key)


def empty_bucket(bucket_name: str) -> None:
    """删除桶内所有对象（删桶前先清空，MinIO 仅允许删空桶）。

    list_objects(recursive=True) 列出所有对象 key，逐个 remove_object。
    best-effort 语义由调用方决定（router 捕获异常不阻断）。
    """
    client = _client()
    for obj in client.list_objects(bucket_name, recursive=True):
        client.remove_object(bucket_name, obj.object_name)


def remove_bucket(bucket_name: str) -> None:
    """删除空分桶。MinIO 仅允许删空桶，非空时 client 抛错——与"非空禁止删"语义天然一致。"""
    client = _client()
    client.remove_bucket(bucket_name)
