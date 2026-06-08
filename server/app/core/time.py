"""时间工具：统一 naive-UTC 时间戳。

全库时间戳字段都用 utcnow()（naive UTC），序列化时 main.py 的补丁补上 "Z" 后缀。
"""

from datetime import UTC, datetime


# 返回不带时区信息的当前 UTC 时间，统一用于所有时间戳字段
def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
