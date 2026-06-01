from __future__ import annotations

import io
import logging
import json

_logger = logging.getLogger(__name__)


def _client():
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
    """创建 bucket（幂等），设置 public-read 策略。"""
    client = _client()
    if not client.bucket_exists(bucket_name):
        client.make_bucket(bucket_name)
    # 设置公开读策略，使图片 URL 永久有效
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
