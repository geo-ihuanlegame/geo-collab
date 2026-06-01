from datetime import datetime, timezone


# 返回不带时区信息的当前 UTC 时间，统一用于所有时间戳字段
def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
