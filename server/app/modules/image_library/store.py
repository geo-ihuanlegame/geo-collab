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
    from minio import Minio

    from server.app.core.config import get_settings

    s = get_settings()
    return Minio(
        s.minio_endpoint,
        access_key=s.minio_access_key,
        secret_key=s.minio_secret_key,
        secure=s.minio_secure,
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


def remove_bucket(bucket_name: str) -> None:
    """删除空分桶。MinIO 仅允许删空桶，非空时 client 抛错——与"非空禁止删"语义天然一致。"""
    client = _client()
    client.remove_bucket(bucket_name)
