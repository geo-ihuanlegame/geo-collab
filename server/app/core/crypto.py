"""敏感凭据静态加密核心（纯函数，无 ORM、无平台逻辑）。

信封：字符串 "enc:v1:<token>"；字节 b"enc:v1:<token>"。前缀是「是否密文」唯一判据。
无密钥时 NullCipher 透传（本地/测试零配置）。底层 Fernet/MultiFernet（AES-128-CBC + HMAC）。
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, MultiFernet

from server.app.core.config import get_settings

_PREFIX = "enc:v1:"
_PREFIX_BYTES = b"enc:v1:"


class Cipher:
    """持有 MultiFernet（或 None＝透传）。第一个密钥加密，全部参与解密。"""

    def __init__(self, fernet: MultiFernet | None) -> None:
        self._fernet = fernet

    def encrypt_str(self, plain: str) -> str:
        if self._fernet is None:
            return plain
        token = self._fernet.encrypt(plain.encode("utf-8")).decode("ascii")
        return _PREFIX + token

    def decrypt_str(self, value: str) -> str:
        if not value.startswith(_PREFIX):
            return value  # 遗留明文透传
        if self._fernet is None:
            raise RuntimeError("遇到加密数据但未配置 GEO_SECRET_KEY / GEO_SECRET_KEYS")
        token = value[len(_PREFIX) :]
        return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")

    def encrypt_bytes(self, plain: bytes) -> bytes:
        if self._fernet is None:
            return plain
        return _PREFIX_BYTES + self._fernet.encrypt(plain)

    def decrypt_bytes(self, value: bytes) -> bytes:
        if not value.startswith(_PREFIX_BYTES):
            return value  # 遗留明文透传
        if self._fernet is None:
            raise RuntimeError("遇到加密数据但未配置 GEO_SECRET_KEY / GEO_SECRET_KEYS")
        return self._fernet.decrypt(value[len(_PREFIX_BYTES) :])


def is_encrypted(value: str | bytes) -> bool:
    if isinstance(value, str):
        return value.startswith(_PREFIX)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).startswith(_PREFIX_BYTES)
    return False


def _configured_keys() -> list[str]:
    settings = get_settings()
    if settings.secret_keys.strip():
        return [k.strip() for k in settings.secret_keys.split(",") if k.strip()]
    if settings.secret_key.strip():
        return [settings.secret_key.strip()]
    return []


@lru_cache
def get_cipher() -> Cipher:
    keys = _configured_keys()
    if not keys:
        return Cipher(None)
    return Cipher(MultiFernet([Fernet(k.encode("ascii")) for k in keys]))


def encrypt_str(plain: str) -> str:
    return get_cipher().encrypt_str(plain)


def decrypt_str(value: str) -> str:
    return get_cipher().decrypt_str(value)


def encrypt_bytes(plain: bytes) -> bytes:
    return get_cipher().encrypt_bytes(plain)


def decrypt_bytes(value: bytes) -> bytes:
    return get_cipher().decrypt_bytes(value)
