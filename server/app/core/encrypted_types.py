"""SQLAlchemy 加密列类型：换列类型即透明加解密，业务代码无感。

EncryptedJSON: Python dict ↔ 加密 JSON 文本（物理列 TEXT）。
EncryptedText: Python str  ↔ 加密文本（物理列 TEXT，未来纯字符串敏感列预留）。
读路径认 enc:v1: 前缀；无前缀＝迁移前遗留明文，直接解析（向后兼容）。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from server.app.core.crypto import decrypt_str, encrypt_str


class EncryptedJSON(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return encrypt_str(json.dumps(value, ensure_ascii=False))

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if not value:
            return None
        return json.loads(decrypt_str(value))


class EncryptedText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return encrypt_str(value)

    def process_result_value(self, value: Any, dialect: Any) -> str | None:
        if not value:
            return None
        return decrypt_str(value)
