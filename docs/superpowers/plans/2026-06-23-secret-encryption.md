# 账号敏感凭据静态加密 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给平台媒体账号的敏感凭据（公众号 `app_secret`、`access_token`、浏览器登录态 `storage_state.json`）加一层通用、可扩展的应用层对称加密，防 DB / 备份泄露。

**Architecture:** 三层（方案 A）：① `core/crypto.py` 纯函数加密核心（Fernet/MultiFernet + `enc:v1:` 自描述信封 + 无密钥 NullCipher 透传）；② `core/encrypted_types.py` 的 `EncryptedJSON`/`EncryptedText` SQLAlchemy `TypeDecorator`，DB 列换类型即透明加解密；③ `accounts/secret_files.py` 的 `read_state`/`write_state` 包住 `storage_state.json` 的显式文件加解密。生产已有明文数据走「列类型迁移 → 新代码 → 幂等回填脚本」零停机迁移，读路径全程向后兼容（认 `enc:v1:` 前缀，无前缀＝遗留明文透传）。

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy / Alembic / `cryptography`（Fernet，已随 `python-jose[cryptography]==3.4.0` 安装，当前 45.0.7）/ pytest（MySQL only）。

## Global Constraints

> 每个任务的要求都隐含包含本节。值逐字照抄自 spec / CLAUDE.md。

- **设计来源**：`docs/superpowers/specs/2026-06-23-secret-encryption-design.md`。改动前以 spec 为准。
- **威胁模型**：防 DB / 备份泄露。密钥走环境变量，不进 DB。**不做** `profile/` 目录加密（交盘级加密）、不上 KMS、不动 `User.password_hash`（已 bcrypt）。
- **加密信封**：字符串用 `enc:v1:` 前缀 + base64 Fernet token；文件用 `enc:v1:` 字节前缀 + Fernet token bytes。前缀是「是否密文」的唯一判据。
- **无密钥＝透传**：未配 `GEO_SECRET_KEY`/`GEO_SECRET_KEYS` 时 NullCipher 原样返回，本地 / 测试 / CI 零配置，**现有全部测试必须保持绿、零改动通过**。
- **配置**：pydantic-settings，前缀 `GEO_`，`get_settings()` 走 `@lru_cache`；`get_cipher()` 同样 `@lru_cache`。测试改 env 后必须 `get_settings.cache_clear()` + `get_cipher.cache_clear()`。
- **数据库**：MySQL only。DB 测试需 `GEO_TEST_DATABASE_URL`（库名含 `"test"`），标 `@pytest.mark.mysql`，用 `build_test_app(monkeypatch)` 且 `finally` 里 `test_app.cleanup()`。纯函数测试无需 DB。
- **异常**：service 层抛命名异常（`ClientError` 家族），不抛裸 `ValueError`。
- **门禁**：`ruff check server/`（E/F/I/B/UP，line-length=100，忽略 E501/B008）+ `ruff format --check server/` + `mypy server/app` + `pytest` 必须全过。
- **迁移协调**：本迁移先落、成新 head。实施时确认 `server/alembic/versions/` 最新 head 仍是 `0048`，是则新迁移用 `0049`、`down_revision="0048"`；若 head 已变，改用最新版本号 + 对应 `down_revision`（**不写死、以实际为准**）。并行的 `2026-06-23-publish-network-retry-design` 迁移后落、由对方 rebase。
- **列类型迁移必须先于加密写入**：MySQL `JSON` 列拒绝非 JSON 的密文串。部署顺序固定为：`alembic upgrade`（JSON→TEXT）→ 启新代码 → 跑回填脚本。

---

## File Structure

**新增**

- `server/app/core/crypto.py` — 加密核心：`Cipher` 类 + `get_cipher()` + 模块级 `encrypt_str`/`decrypt_str`/`encrypt_bytes`/`decrypt_bytes`/`is_encrypted`。
- `server/app/core/encrypted_types.py` — `EncryptedJSON` / `EncryptedText`（SQLAlchemy `TypeDecorator`）。
- `server/app/modules/accounts/secret_files.py` — `read_state(path)` / `write_state(path, state)`。
- `server/scripts/gen_secret_key.py` — 打印一个新 Fernet 密钥。
- `server/scripts/encrypt_secrets.py` — 幂等回填（DB 字段 + storage_state 文件）。
- `server/alembic/versions/0049_encrypt_account_secret_columns.py` — `api_credentials`/`api_token_cache` JSON→TEXT。
- 测试：`server/tests/test_crypto.py`、`test_secret_files.py`、`test_encrypted_column.py`、`test_encrypt_secrets_backfill.py`。

**改动**

- `server/app/core/config.py` — `secret_key` / `secret_keys` 设置。
- `server/app/modules/accounts/models.py` — 两列换 `EncryptedJSON`。
- `server/app/modules/accounts/auth.py` — storage_state 读写（`:1212`/`:1239`/`:1329`）+ 导出（`:1294-1299`）/ 导入（`:1402`）。
- `server/app/modules/accounts/login_broker.py` — `_pw_storage_state`（`:127-128`）。
- `server/app/modules/tasks/drivers/toutiao.py` — 发布后回存（`:1072`）。
- `server/tests/test_accounts_import_export.py` — 扩展明文/密文断言。
- `requirements.txt` — 显式声明 `cryptography`。
- `CLAUDE.md` + `.env.example` — 密钥配置、丢钥代价、profile 交盘级加密、PlatformDriver 用 `write_state`/`read_state` 约定。

---

## Task 1: 加密核心 `core/crypto.py` + 配置 + 密钥生成脚本

**Files:**
- Create: `server/app/core/crypto.py`
- Create: `server/scripts/gen_secret_key.py`
- Modify: `server/app/core/config.py`（`Settings` 类内加两行；位置参照现有 `mcp_token` 附近）
- Modify: `requirements.txt`（显式加 `cryptography`）
- Test: `server/tests/test_crypto.py`

**Interfaces:**
- Produces:
  - `server.app.core.crypto.encrypt_str(plain: str) -> str` — 有密钥时返回 `"enc:v1:<token>"`，无密钥原样返回。
  - `server.app.core.crypto.decrypt_str(value: str) -> str` — 认 `enc:v1:` 前缀解密；无前缀原样返回（遗留明文）；有前缀但无密钥抛 `RuntimeError`。
  - `server.app.core.crypto.encrypt_bytes(plain: bytes) -> bytes` / `decrypt_bytes(value: bytes) -> bytes` — 字节版，前缀 `b"enc:v1:"`。
  - `server.app.core.crypto.is_encrypted(value: str | bytes) -> bool`
  - `server.app.core.crypto.get_cipher()` — `@lru_cache` 单例；测试用 `get_cipher.cache_clear()`。
  - `server.app.core.config.Settings.secret_key: str` / `secret_keys: str`

- [ ] **Step 1: 写失败测试** `server/tests/test_crypto.py`

```python
"""crypto 核心纯函数测试（无需 DB）。"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from server.app.core import crypto
from server.app.core.config import get_settings


def _set_key(monkeypatch, value: str) -> None:
    monkeypatch.setenv("GEO_SECRET_KEY", value)
    get_settings.cache_clear()
    crypto.get_cipher.cache_clear()


def _clear_keys(monkeypatch) -> None:
    monkeypatch.delenv("GEO_SECRET_KEY", raising=False)
    monkeypatch.delenv("GEO_SECRET_KEYS", raising=False)
    get_settings.cache_clear()
    crypto.get_cipher.cache_clear()


def test_roundtrip_str_with_key(monkeypatch):
    _set_key(monkeypatch, Fernet.generate_key().decode("ascii"))
    token = crypto.encrypt_str("super-secret")
    assert token.startswith("enc:v1:")
    assert "super-secret" not in token
    assert crypto.decrypt_str(token) == "super-secret"


def test_roundtrip_bytes_with_key(monkeypatch):
    _set_key(monkeypatch, Fernet.generate_key().decode("ascii"))
    blob = crypto.encrypt_bytes(b'{"cookies": []}')
    assert blob.startswith(b"enc:v1:")
    assert b"cookies" not in blob
    assert crypto.decrypt_bytes(blob) == b'{"cookies": []}'


def test_null_cipher_passthrough_when_no_key(monkeypatch):
    _clear_keys(monkeypatch)
    assert crypto.encrypt_str("x") == "x"
    assert crypto.decrypt_str("x") == "x"
    assert crypto.encrypt_bytes(b"x") == b"x"


def test_decrypt_legacy_plaintext_passthrough(monkeypatch):
    _set_key(monkeypatch, Fernet.generate_key().decode("ascii"))
    # 无 enc: 前缀 = 遗留明文，原样返回
    assert crypto.decrypt_str('{"app_id": "a"}') == '{"app_id": "a"}'
    assert crypto.decrypt_bytes(b'{"cookies": []}') == b'{"cookies": []}'


def test_decrypt_encrypted_without_key_raises(monkeypatch):
    _set_key(monkeypatch, Fernet.generate_key().decode("ascii"))
    token = crypto.encrypt_str("secret")
    _clear_keys(monkeypatch)
    with pytest.raises(RuntimeError):
        crypto.decrypt_str(token)


def test_multifernet_rotation(monkeypatch):
    k_old = Fernet.generate_key().decode("ascii")
    k_new = Fernet.generate_key().decode("ascii")
    # 用旧钥加密
    _set_key(monkeypatch, k_old)
    token = crypto.encrypt_str("rotate-me")
    # 新钥入队首、旧钥保留 → 仍可解；新写入用新钥
    monkeypatch.setenv("GEO_SECRET_KEYS", f"{k_new},{k_old}")
    monkeypatch.delenv("GEO_SECRET_KEY", raising=False)
    get_settings.cache_clear()
    crypto.get_cipher.cache_clear()
    assert crypto.decrypt_str(token) == "rotate-me"


def test_is_encrypted(monkeypatch):
    _set_key(monkeypatch, Fernet.generate_key().decode("ascii"))
    assert crypto.is_encrypted(crypto.encrypt_str("x")) is True
    assert crypto.is_encrypted("plain") is False
    assert crypto.is_encrypted(crypto.encrypt_bytes(b"x")) is True
    assert crypto.is_encrypted(b"plain") is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test python -m pytest server/tests/test_crypto.py -q`
Expected: FAIL（`ModuleNotFoundError: server.app.core.crypto` 或 `AttributeError: secret_key`）

> 注：`test_crypto.py` 不需要 DB，但裸跑 `pytest` 时 `conftest.py` 会按 `GEO_TEST_DATABASE_URL` 决定跳过策略；带上 env 更稳。本机 conda 环境用 `python -m pytest`（见记忆 run-tests-env）。

- [ ] **Step 3: 加配置** — 编辑 `server/app/core/config.py`，在 `Settings` 类里 `mcp_token` 那一行附近加：

```python
    # 敏感凭据静态加密（app_secret / token / storage_state）。
    # 空 = NullCipher 透传（本地/测试零配置）；prod 设密钥才真加密。
    # 密钥 = Fernet urlsafe-base64 32 字节，用 `python -m server.scripts.gen_secret_key` 生成。
    secret_key: str = ""        # GEO_SECRET_KEY，单密钥
    secret_keys: str = ""       # GEO_SECRET_KEYS，逗号分隔多密钥（轮换；非空时优先于 secret_key）
```

- [ ] **Step 4: 写加密核心** — 创建 `server/app/core/crypto.py`：

```python
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
        token = value[len(_PREFIX):]
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
        return self._fernet.decrypt(value[len(_PREFIX_BYTES):])


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
```

- [ ] **Step 5: 跑测试确认通过**

Run: `GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test python -m pytest server/tests/test_crypto.py -q`
Expected: PASS（8 passed）

- [ ] **Step 6: 写密钥生成脚本** — 创建 `server/scripts/gen_secret_key.py`：

```python
"""打印一个新的 Fernet 密钥，填入 GEO_SECRET_KEY。

用法：python -m server.scripts.gen_secret_key
"""

from __future__ import annotations

from cryptography.fernet import Fernet


def main() -> None:
    print(Fernet.generate_key().decode("ascii"))


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: 跑脚本确认输出可用**

Run: `python -m server.scripts.gen_secret_key`
Expected: 输出一行 44 字符 base64 串（结尾 `=`），且 `python -c "from cryptography.fernet import Fernet; Fernet('<上面输出>'.encode())"` 不报错。

- [ ] **Step 8: 显式声明依赖** — 编辑 `requirements.txt`，在 `python-jose[cryptography]==3.4.0` 附近加一行（pin 到项目 env `geo_xzpt` 已装版本）：

```
cryptography==48.0.0
```

- [ ] **Step 9: 门禁 + 提交**

Run: `ruff check server/app/core/crypto.py server/scripts/gen_secret_key.py && ruff format --check server/app/core/crypto.py && mypy server/app/core/crypto.py`
Expected: 全过

```bash
git add server/app/core/crypto.py server/scripts/gen_secret_key.py server/app/core/config.py requirements.txt server/tests/test_crypto.py
git commit -m "feat(security): 加密核心 crypto.py + 密钥配置/生成脚本

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `EncryptedJSON` / `EncryptedText` 类型装饰器

**Files:**
- Create: `server/app/core/encrypted_types.py`
- Test: `server/tests/test_encrypted_column.py`（本任务先写纯单测部分；DB 集成断言在 Task 4 补）

**Interfaces:**
- Consumes: `server.app.core.crypto.encrypt_str` / `decrypt_str`
- Produces:
  - `server.app.core.encrypted_types.EncryptedJSON` — `TypeDecorator`，`impl=Text`，Python dict ↔ 加密 JSON 文本。
  - `server.app.core.encrypted_types.EncryptedText` — `TypeDecorator`，`impl=Text`，Python str ↔ 加密文本（未来纯字符串敏感列预留）。

- [ ] **Step 1: 写失败测试** `server/tests/test_encrypted_column.py`

```python
"""EncryptedJSON / EncryptedText 的 bind/result 处理纯单测（无需 DB）。"""

from __future__ import annotations

from cryptography.fernet import Fernet

from server.app.core import crypto
from server.app.core.config import get_settings
from server.app.core.encrypted_types import EncryptedJSON, EncryptedText


def _set_key(monkeypatch) -> None:
    monkeypatch.setenv("GEO_SECRET_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()
    crypto.get_cipher.cache_clear()


def test_encrypted_json_bind_then_result_roundtrip(monkeypatch):
    _set_key(monkeypatch)
    col = EncryptedJSON()
    stored = col.process_bind_param({"app_id": "a", "app_secret": "s"}, dialect=None)
    assert stored.startswith("enc:v1:")
    assert "app_secret" not in stored
    assert col.process_result_value(stored, dialect=None) == {"app_id": "a", "app_secret": "s"}


def test_encrypted_json_none_passthrough(monkeypatch):
    _set_key(monkeypatch)
    col = EncryptedJSON()
    assert col.process_bind_param(None, dialect=None) is None
    assert col.process_result_value(None, dialect=None) is None


def test_encrypted_json_reads_legacy_plaintext(monkeypatch):
    _set_key(monkeypatch)
    col = EncryptedJSON()
    # 迁移前的明文 JSON 文本（无 enc: 前缀）应能直接读出
    assert col.process_result_value('{"app_id": "a"}', dialect=None) == {"app_id": "a"}


def test_encrypted_text_roundtrip(monkeypatch):
    _set_key(monkeypatch)
    col = EncryptedText()
    stored = col.process_bind_param("token-123", dialect=None)
    assert stored.startswith("enc:v1:")
    assert col.process_result_value(stored, dialect=None) == "token-123"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_encrypted_column.py -q`
Expected: FAIL（`ModuleNotFoundError: server.app.core.encrypted_types`）

- [ ] **Step 3: 写类型装饰器** — 创建 `server/app/core/encrypted_types.py`：

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_encrypted_column.py -q`
Expected: PASS（4 passed）

- [ ] **Step 5: 门禁 + 提交**

Run: `ruff check server/app/core/encrypted_types.py && ruff format --check server/app/core/encrypted_types.py && mypy server/app/core/encrypted_types.py`
Expected: 全过

```bash
git add server/app/core/encrypted_types.py server/tests/test_encrypted_column.py
git commit -m "feat(security): EncryptedJSON/EncryptedText 透明加密列类型

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: storage_state 文件助手 `secret_files.py`

**Files:**
- Create: `server/app/modules/accounts/secret_files.py`
- Test: `server/tests/test_secret_files.py`

**Interfaces:**
- Consumes: `server.app.core.crypto.encrypt_bytes` / `decrypt_bytes`
- Produces:
  - `server.app.modules.accounts.secret_files.read_state(path: pathlib.Path) -> dict` — 读文件→认前缀解密→`json.loads`；无前缀＝遗留明文直接 load。
  - `server.app.modules.accounts.secret_files.write_state(path: pathlib.Path, state: dict) -> None` — `json.dumps`→加密→落盘（自动建父目录）。

- [ ] **Step 1: 写失败测试** `server/tests/test_secret_files.py`

```python
"""storage_state 文件加解密助手测试（无需 DB）。"""

from __future__ import annotations

import json

from cryptography.fernet import Fernet

from server.app.core import crypto
from server.app.core.config import get_settings
from server.app.modules.accounts import secret_files


def _set_key(monkeypatch) -> None:
    monkeypatch.setenv("GEO_SECRET_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()
    crypto.get_cipher.cache_clear()


def _clear_keys(monkeypatch) -> None:
    monkeypatch.delenv("GEO_SECRET_KEY", raising=False)
    monkeypatch.delenv("GEO_SECRET_KEYS", raising=False)
    get_settings.cache_clear()
    crypto.get_cipher.cache_clear()


def test_write_then_read_roundtrip(monkeypatch, tmp_path):
    _set_key(monkeypatch)
    state = {"cookies": [{"name": "sid", "value": "secret-cookie"}], "origins": []}
    p = tmp_path / "sub" / "storage_state.json"
    secret_files.write_state(p, state)
    # 磁盘上是密文，不含明文 cookie
    assert b"secret-cookie" not in p.read_bytes()
    assert p.read_bytes().startswith(b"enc:v1:")
    assert secret_files.read_state(p) == state


def test_read_legacy_plaintext(monkeypatch, tmp_path):
    _set_key(monkeypatch)
    state = {"cookies": [], "origins": []}
    p = tmp_path / "storage_state.json"
    p.write_text(json.dumps(state), encoding="utf-8")  # 明文写入（模拟迁移前）
    assert secret_files.read_state(p) == state


def test_passthrough_without_key(monkeypatch, tmp_path):
    _clear_keys(monkeypatch)
    state = {"cookies": [], "origins": []}
    p = tmp_path / "storage_state.json"
    secret_files.write_state(p, state)
    assert p.read_bytes().startswith(b"{")  # 无密钥＝明文落盘
    assert secret_files.read_state(p) == state
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest server/tests/test_secret_files.py -q`
Expected: FAIL（`ModuleNotFoundError: ...secret_files`）

- [ ] **Step 3: 写助手** — 创建 `server/app/modules/accounts/secret_files.py`：

```python
"""storage_state.json 的加密读写助手。

Playwright 的 storage_state 既接受文件路径也接受 dict——加密版读出内存 dict 喂给
new_context，不明文落盘。无密钥时 crypto 层透传，等价明文读写。
"""

from __future__ import annotations

import json
from pathlib import Path

from server.app.core.crypto import decrypt_bytes, encrypt_bytes


def read_state(path: Path) -> dict:
    raw = path.read_bytes()
    return json.loads(decrypt_bytes(raw).decode("utf-8"))


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    plain = json.dumps(state, ensure_ascii=False).encode("utf-8")
    path.write_bytes(encrypt_bytes(plain))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest server/tests/test_secret_files.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 门禁 + 提交**

Run: `ruff check server/app/modules/accounts/secret_files.py && ruff format --check server/app/modules/accounts/secret_files.py && mypy server/app/modules/accounts/secret_files.py`
Expected: 全过

```bash
git add server/app/modules/accounts/secret_files.py server/tests/test_secret_files.py
git commit -m "feat(security): storage_state 加密读写助手 secret_files

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: DB 列换类型 + Alembic 迁移 + 集成断言

**Files:**
- Modify: `server/app/modules/accounts/models.py:55-60`（两列换 `EncryptedJSON`）
- Create: `server/alembic/versions/0049_encrypt_account_secret_columns.py`
- Test: `server/tests/test_encrypted_column.py`（追加 1 个 `@pytest.mark.mysql` 集成断言）

**Interfaces:**
- Consumes: `server.app.core.encrypted_types.EncryptedJSON`
- Produces: `Account.api_credentials` / `Account.api_token_cache` 在 DB 中以 `enc:v1:` 文本存储（设密钥时），ORM 读出仍是 dict。

- [ ] **Step 1: 追加 mysql 集成测试** — 在 `server/tests/test_encrypted_column.py` 末尾加：

```python
import pytest
from sqlalchemy import text as sa_text


@pytest.mark.mysql
def test_api_credentials_encrypted_at_rest(monkeypatch):
    monkeypatch.setenv("GEO_SECRET_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()
    crypto.get_cipher.cache_clear()
    from server.app.modules.system.models import Platform
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            if db.query(Platform).filter(Platform.code == "wechat_mp").first() is None:
                db.add(Platform(code="wechat_mp", name="微信公众号",
                                base_url="https://mp.weixin.qq.com", enabled=True))
                db.commit()
        # 经真实 API 建号 → 走 ORM 加密写入路径
        resp = test_app.client.post("/api/accounts", json={
            "platform_code": "wechat_mp",
            "display_name": "加密测试号",
            "api_credentials": {"app_id": "wxappid123456", "app_secret": "shh-secret"},
        })
        assert resp.status_code == 200, resp.text
        # API 回包不含 secret 原文，只回尾 4 位
        assert resp.json()["app_secret_tail"] == "cret"
        # 原始 DB cell 是密文
        with test_app.session_factory() as db:
            raw = db.execute(sa_text(
                "SELECT api_credentials FROM accounts WHERE platform_user_id = 'wxappid123456'"
            )).scalar_one()
        assert raw.startswith("enc:v1:")
        assert "shh-secret" not in raw
    finally:
        test_app.cleanup()
```

> `build_test_app`（在 `server/tests/utils.py`）用 `create_all` 建表，列类型取 models 定义（已是 TEXT），故本测试不依赖 Alembic 迁移。`test_app.session_factory()` 是 session 上下文管理器、`test_app.client` 是带 admin JWT 的测试客户端（接口参照 `test_accounts_api_wechat.py`）。`GEO_SECRET_KEY` 在 `build_test_app` 前 setenv 并清 cache，确保写入即加密。

- [ ] **Step 2: 跑测试确认失败**

Run: `GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test python -m pytest server/tests/test_encrypted_column.py::test_api_credentials_encrypted_at_rest -q`
Expected: FAIL（断言 `raw.startswith("enc:v1:")` 失败——列还是 JSON、存的是明文）

- [ ] **Step 3: 换列类型** — 编辑 `server/app/modules/accounts/models.py`，把 import 与两列改掉：

顶部 import 区加：

```python
from server.app.core.encrypted_types import EncryptedJSON
```

`:55-60` 两列（保留 `MutableDict` 包裹与注释）：

```python
    api_credentials: Mapped[dict | None] = mapped_column(
        MutableDict.as_mutable(EncryptedJSON()), nullable=True
    )  # {"app_id": ..., "app_secret": ...}；加密存储，永不通过 API 回传原文
    api_token_cache: Mapped[dict | None] = mapped_column(
        MutableDict.as_mutable(EncryptedJSON()), nullable=True
    )  # {"access_token": ..., "expires_at": <epoch秒>}；加密存储，web/worker 跨进程共享
```

- [ ] **Step 4: 跑测试确认通过**

Run: `GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test python -m pytest server/tests/test_encrypted_column.py -q`
Expected: PASS（含新集成测试）

- [ ] **Step 5: 写 Alembic 迁移** — 创建 `server/alembic/versions/0049_encrypt_account_secret_columns.py`（**先确认 `0048` 仍是 head**，否则改版本号/`down_revision`）：

```python
"""accounts.api_credentials / api_token_cache 改 TEXT（承载 enc:v1: 密文）。

数据加密由幂等脚本 server.scripts.encrypt_secrets 完成，本迁移只改列类型。
JSON→TEXT 后 MySQL 把原值转成 JSON 文本表示，向后兼容读（无 enc: 前缀＝明文）。

修订 ID: 0049
上一修订: 0048
创建日期: 2026-06-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("api_credentials", "api_token_cache")


def _col_type_name(inspector, column: str) -> str:
    for col in inspector.get_columns("accounts"):
        if col["name"] == column:
            return type(col["type"]).__name__.upper()
    return ""


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    for column in _COLUMNS:
        if "JSON" in _col_type_name(inspector, column):
            op.alter_column(
                "accounts",
                column,
                existing_type=sa.JSON(),
                type_=sa.Text(),
                existing_nullable=True,
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    for column in _COLUMNS:
        if "JSON" not in _col_type_name(inspector, column):
            op.alter_column(
                "accounts",
                column,
                existing_type=sa.Text(),
                type_=sa.JSON(),
                existing_nullable=True,
            )
```

> downgrade 把 TEXT 转回 JSON：若此时列里仍有 `enc:v1:` 密文，MySQL 会因非法 JSON 报错——所以 downgrade 前必须先解密回填（运维手册写明）。本期不为 downgrade 自动解密（YAGNI）。

- [ ] **Step 6: 验证迁移可上可下（用一次性 scratch 库，绝不碰本地 `geo_collab`）**

`alembic.ini` 在**仓库根** `e:\geo`（不是 `server/`）——在根目录跑 alembic。本地 `geo_collab` 是开发全栈在用的库、迁移头停在旧版本，**禁止**对它 `alembic upgrade`。改用临时库从 base 跑全链验证：

```bash
PY="/c/Users/Administrator/miniconda3/envs/geo_xzpt/python.exe"
# 1) 建 scratch 库
"$PY" -c "import pymysql; c=pymysql.connect(host='127.0.0.1',port=3306,user='geo_user',password='GeoUser20260513A1'); c.cursor().execute('DROP DATABASE IF EXISTS geo_scratch_mig'); c.cursor().execute('CREATE DATABASE geo_scratch_mig'); c.commit()"
# 2) 从 base 跑到 head（含新 0049），再 downgrade -1，再 upgrade head
GEO_DATABASE_URL="mysql+pymysql://geo_user:GeoUser20260513A1@127.0.0.1:3306/geo_scratch_mig" "$PY" -m alembic upgrade head
GEO_DATABASE_URL="mysql+pymysql://geo_user:GeoUser20260513A1@127.0.0.1:3306/geo_scratch_mig" "$PY" -c "import pymysql; cur=pymysql.connect(host='127.0.0.1',port=3306,user='geo_user',password='GeoUser20260513A1',database='geo_scratch_mig').cursor(); cur.execute(\"SHOW COLUMNS FROM accounts LIKE 'api_credentials'\"); print(cur.fetchone())"
GEO_DATABASE_URL="mysql+pymysql://geo_user:GeoUser20260513A1@127.0.0.1:3306/geo_scratch_mig" "$PY" -m alembic downgrade -1
GEO_DATABASE_URL="mysql+pymysql://geo_user:GeoUser20260513A1@127.0.0.1:3306/geo_scratch_mig" "$PY" -m alembic upgrade head
# 3) 删 scratch 库
"$PY" -c "import pymysql; pymysql.connect(host='127.0.0.1',port=3306,user='geo_user',password='GeoUser20260513A1').cursor().execute('DROP DATABASE geo_scratch_mig')"
```
Expected: `upgrade head` 无错；`SHOW COLUMNS` 输出第二列（Type）为 `text`；`downgrade -1` 把它变回 `json`；再 `upgrade head` 无错。

> 若从 base 的全链 upgrade 因**与 0049 无关**的历史迁移问题失败，退而用 `GEO_DATABASE_URL=...geo_scratch_mig "$PY" -m alembic upgrade 0048:head --sql` 离线生成 SQL，人工核对 0049 段产出 `ALTER TABLE accounts MODIFY api_credentials ... TEXT`（两列），并在报告里说明走了哪条路径 + 证据。无论哪条路径，结束都要 DROP 掉 scratch 库。

- [ ] **Step 7: 跑账号相关回归确认未破坏**

Run: `GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test python -m pytest server/tests/test_accounts_api.py server/tests/test_accounts_api_wechat.py -q`
Expected: PASS（透明加解密，业务逻辑不变）

- [ ] **Step 8: 门禁 + 提交**

Run: `ruff check server/app/modules/accounts/models.py server/alembic/versions/0049_encrypt_account_secret_columns.py && mypy server/app/modules/accounts/models.py`
Expected: 全过

```bash
git add server/app/modules/accounts/models.py server/alembic/versions/0049_encrypt_account_secret_columns.py server/tests/test_encrypted_column.py
git commit -m "feat(security): api_credentials/api_token_cache 透明加密 + JSON→TEXT 迁移

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: 接入 storage_state 文件 I/O + 导入导出 ZIP

**Files:**
- Modify: `server/app/modules/accounts/auth.py`（`:1212` 读、`:1239` 写、`:1294-1299` 导出、`:1318-1343` `_assess_imported_status`、`:1402` 导入）
- Modify: `server/app/modules/accounts/login_broker.py:127-128`
- Modify: `server/app/modules/tasks/drivers/toutiao.py:1072`
- Test: `server/tests/test_accounts_import_export.py`（扩展）

**Interfaces:**
- Consumes: `server.app.modules.accounts.secret_files.read_state` / `write_state`

- [ ] **Step 1: 扩展导入导出测试** — 在 `server/tests/test_accounts_import_export.py` 找到现有导出→导入往返用例，加一个设密钥变体（若文件结构不同，新增一个独立 `@pytest.mark.mysql` 用例）。断言两点：① 导出 ZIP 内 `storage_state.json` 是**明文**（可被另一实例导入）；② 导入后落盘文件是**密文**。

```python
import json
import zipfile
import io

import pytest
from cryptography.fernet import Fernet

from server.app.core import crypto
from server.app.core.config import get_settings


@pytest.mark.mysql
def test_export_plaintext_import_reencrypts(monkeypatch):
    monkeypatch.setenv("GEO_SECRET_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()
    crypto.get_cipher.cache_clear()
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        # 1) 造一个带 storage_state 的浏览器账号（复用文件里现有 helper 造号 + 写 state 文件，
        #    写文件务必用 secret_files.write_state，使磁盘为密文）
        from server.app.modules.accounts import secret_files
        # ...（按本文件既有 setup 造 account + state_path，state 内容含可识别 cookie 值 "marker-cookie"）...
        # state_file = <data_dir>/<account.state_path>
        # secret_files.write_state(state_file, {"cookies": [{"name": "s", "value": "marker-cookie"}], "origins": []})

        # 2) 导出
        from server.app.modules.accounts.auth import export_accounts_auth_package
        export_path = export_accounts_auth_package(db, export_request)  # 按既有签名
        with zipfile.ZipFile(export_path) as z:
            name = next(n for n in z.namelist() if n.endswith("storage_state.json"))
            zipped = z.read(name)
        # ZIP 内是明文
        assert b"marker-cookie" in zipped
        assert json.loads(zipped)["cookies"][0]["value"] == "marker-cookie"

        # 3) 导入到新 user，落盘是密文
        from server.app.modules.accounts.auth import import_accounts_auth_package
        with open(export_path, "rb") as f:
            import_accounts_auth_package(db, other_user_id, f.read())
        # 找到导入后的 dest 文件，读原始字节断言密文
        # assert dest.read_bytes().startswith(b"enc:v1:")
        # assert b"marker-cookie" not in dest.read_bytes()
        # 经 read_state 仍能读回明文
        assert secret_files.read_state(dest)["cookies"][0]["value"] == "marker-cookie"
    finally:
        test_app.cleanup()
```

> 这是带 `...` 占位的脚手架——实施时按 `test_accounts_import_export.py` 现有的造号 / 路径 helper 补全 `db` / `export_request` / `other_user_id` / `dest`。**断言四条不可省**：ZIP 明文含 marker、导入后落盘 `enc:v1:` 前缀、落盘不含 marker、`read_state` 能读回。

- [ ] **Step 2: 跑测试确认失败**

Run: `GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test python -m pytest server/tests/test_accounts_import_export.py -q -k reencrypt`
Expected: FAIL（导入仍 `write_bytes` 原字节、未加密；导出仍 `archive.write` 密文文件）

- [ ] **Step 3: 改 auth.py 读点（`:1212`）** — `_check_login_state_headless`（或所在函数）里：

```python
        context = browser.new_context(
            storage_state=read_state(abs_state_path), viewport=viewport
        )
```

并在 auth.py 顶部 import 区加：

```python
from server.app.modules.accounts.secret_files import read_state, write_state
```

> `abs_state_path` 是 `Path`，`read_state` 接受 `Path`。Playwright `new_context(storage_state=<dict>)` 合法。

- [ ] **Step 4: 改 auth.py 写点（`:1239`）**

```python
            write_state(abs_state_path, context.storage_state())
```

- [ ] **Step 5: 改 auth.py `_assess_imported_status`（`:1328-1331`）**

```python
    try:
        data = read_state(state_path)
    except Exception:
        return "unknown"
```

- [ ] **Step 6: 改 auth.py 导出（`:1294-1304`）** — 把 `archive.write(state_file, ...)` 换成解密后写明文：

```python
            if account.state_path:
                try:
                    state_file = _resolve_data_file(account.state_path)
                    state_archive_path = f"{account_dir}/storage_state.json"
                    archive.writestr(
                        state_archive_path,
                        json.dumps(read_state(state_file), ensure_ascii=False, indent=2),
                    )
                    exported_files.append(state_archive_path)
                except (ClientError, OSError):
                    _logger.warning(
                        "Skipping storage_state.json for account %s - file not found/unreadable",
                        account.display_name,
                    )
```

- [ ] **Step 7: 改 auth.py 导入（`:1402`）** — 把 `dest.write_bytes(archive.read(...))` 换成解析后重新加密落盘：

```python
            dest.parent.mkdir(parents=True, exist_ok=True)
            state_obj = json.loads(archive.read(archive_state_path).decode("utf-8"))
            write_state(dest, state_obj)
```

> ZIP 内恒为明文 JSON（导出已解密），故 `json.loads` 安全；`write_state` 用本地密钥重新加密。`_assess_imported_status(dest)`（后续调用）已改 `read_state`，自洽。

- [ ] **Step 8: 改 login_broker.py（`:127-128`）**

```python
async def _pw_storage_state(context: Any, state_path: Path) -> None:
    state = await context.storage_state()
    write_state(state_path, state)
```

并在 login_broker.py 顶部 import 区加：

```python
from server.app.modules.accounts.secret_files import write_state
```

> `context.storage_state()` 在 async API 下返回 dict（无 `path=` 即返回值）；`write_state` 是同步纯字节写，在 async 函数里直接调用可接受（写盘量极小，与既有同步落盘一致）。

- [ ] **Step 9: 改 toutiao.py（`:1072`）**

```python
        with publish_step("save storage state"):
            write_state(Path(payload.state_path), context.storage_state())
```

并在 toutiao.py 顶部 import 区加（若未 import `Path`）：

```python
from pathlib import Path

from server.app.modules.accounts.secret_files import write_state
```

> 驱动按约定不 import 文章 / 账号 ORM，但 `secret_files` 是无 ORM 的纯加密助手，等同 import `core` 工具，允许。`payload.state_path` 是相对/绝对路径字符串，包成 `Path`。

- [ ] **Step 10: 跑测试确认通过**

Run: `GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test python -m pytest server/tests/test_accounts_import_export.py -q`
Expected: PASS

- [ ] **Step 11: 跑发布 / 登录相关回归**

Run: `GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test python -m pytest server/tests/test_publish_web_no_browser.py server/tests/test_login_broker.py server/tests/test_accounts_import_export.py -q`
Expected: PASS（无密钥时透传，行为不变）

- [ ] **Step 12: 门禁 + 提交**

Run: `ruff check server/app/modules/accounts/auth.py server/app/modules/accounts/login_broker.py server/app/modules/tasks/drivers/toutiao.py && ruff format --check server/app/modules/accounts/auth.py && mypy server/app/modules/accounts/auth.py`
Expected: 全过

```bash
git add server/app/modules/accounts/auth.py server/app/modules/accounts/login_broker.py server/app/modules/tasks/drivers/toutiao.py server/tests/test_accounts_import_export.py
git commit -m "feat(security): storage_state 文件加密接入 6 处 I/O + 导入导出 ZIP 明文便携

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: 幂等回填脚本 `encrypt_secrets.py`

**Files:**
- Create: `server/scripts/encrypt_secrets.py`
- Test: `server/tests/test_encrypt_secrets_backfill.py`

**Interfaces:**
- Consumes: `server.app.core.crypto.encrypt_str` / `is_encrypted`、`server.app.modules.accounts.secret_files.read_state` / `write_state`、`server.app.db.session.SessionLocal`、`get_data_dir`
- Produces:
  - `server.scripts.encrypt_secrets.backfill_db(session) -> int` — 加密 accounts 两列未加密行，返回改动行数。
  - `server.scripts.encrypt_secrets.backfill_files(data_dir: Path) -> int` — 加密未加密的 storage_state 文件，返回改动文件数。
  - `server.scripts.encrypt_secrets.main() -> None`

- [ ] **Step 1: 写失败测试** `server/tests/test_encrypt_secrets_backfill.py`

```python
"""回填脚本：DB 行 + 文件，幂等。"""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text as sa_text

from server.app.core import crypto
from server.app.core.config import get_settings
from server.app.modules.accounts import secret_files
from server.scripts import encrypt_secrets


def _set_key(monkeypatch) -> None:
    monkeypatch.setenv("GEO_SECRET_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()
    crypto.get_cipher.cache_clear()


def test_backfill_files_idempotent(monkeypatch, tmp_path):
    _set_key(monkeypatch)
    bs = tmp_path / "browser_states" / "toutiao" / "acc"
    bs.mkdir(parents=True)
    f = bs / "storage_state.json"
    f.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")  # 明文

    changed1 = encrypt_secrets.backfill_files(tmp_path)
    assert changed1 == 1
    assert f.read_bytes().startswith(b"enc:v1:")
    # 再跑一遍：已加密，0 改动（幂等）
    changed2 = encrypt_secrets.backfill_files(tmp_path)
    assert changed2 == 0
    assert secret_files.read_state(f) == {"cookies": [], "origins": []}


@pytest.mark.mysql
def test_backfill_db_idempotent(monkeypatch):
    _set_key(monkeypatch)
    from server.app.modules.system.models import Platform
    from server.tests.utils import build_test_app

    test_app = build_test_app(monkeypatch)
    try:
        with test_app.session_factory() as db:
            if db.query(Platform).filter(Platform.code == "wechat_mp").first() is None:
                db.add(Platform(code="wechat_mp", name="微信公众号",
                                base_url="https://mp.weixin.qq.com", enabled=True))
                db.commit()
        # 经 API 建号（ORM 已加密），再用原始 SQL 改回明文，模拟迁移前的遗留明文行
        resp = test_app.client.post("/api/accounts", json={
            "platform_code": "wechat_mp",
            "display_name": "回填测试号",
            "api_credentials": {"app_id": "wxbackfill01", "app_secret": "plain-secret"},
        })
        assert resp.status_code == 200, resp.text
        with test_app.session_factory() as db:
            db.execute(
                sa_text("UPDATE accounts SET api_credentials = :c "
                        "WHERE platform_user_id = 'wxbackfill01'"),
                {"c": json.dumps({"app_id": "wxbackfill01", "app_secret": "plain-secret"})},
            )
            db.commit()

        with test_app.session_factory() as db:
            assert encrypt_secrets.backfill_db(db) == 1
        with test_app.session_factory() as db:
            raw = db.execute(sa_text(
                "SELECT api_credentials FROM accounts WHERE platform_user_id = 'wxbackfill01'"
            )).scalar_one()
        assert raw.startswith("enc:v1:")
        assert "plain-secret" not in raw
        # 幂等：再跑 0 改动
        with test_app.session_factory() as db:
            assert encrypt_secrets.backfill_db(db) == 0
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test python -m pytest server/tests/test_encrypt_secrets_backfill.py -q`
Expected: FAIL（`ModuleNotFoundError: server.scripts.encrypt_secrets`）

- [ ] **Step 3: 写回填脚本** — 创建 `server/scripts/encrypt_secrets.py`：

```python
"""幂等回填：把明文 api_credentials / api_token_cache（DB）+ storage_state.json（文件）
就地加密。以 enc:v1: 前缀判断，可重跑。

用法（先确保 GEO_SECRET_KEY 已设、迁移已 upgrade head、新代码已部署）：
    python -m server.scripts.encrypt_secrets
"""

from __future__ import annotations

import json
from pathlib import Path

# 独立运行需导入全部 models 触发 mapper 配置（同 seed_users.py）
import server.app.modules.accounts.models  # noqa: F401
import server.app.modules.ai_generation.models  # noqa: F401
import server.app.modules.articles.models  # noqa: F401
import server.app.modules.audit.models  # noqa: F401
import server.app.modules.image_library.models  # noqa: F401
import server.app.modules.prompt_templates.models  # noqa: F401
import server.app.modules.skills.models  # noqa: F401
import server.app.modules.tasks.models  # noqa: F401
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from server.app.core.crypto import encrypt_str, is_encrypted
from server.app.db.session import SessionLocal, get_data_dir
from server.app.modules.accounts.secret_files import write_state

_COLUMNS = ("api_credentials", "api_token_cache")


def backfill_db(session: Session) -> int:
    rows = session.execute(
        sa_text("SELECT id, api_credentials, api_token_cache FROM accounts")
    ).all()
    changed = 0
    for row in rows:
        sets = {}
        for col, raw in zip(_COLUMNS, (row.api_credentials, row.api_token_cache)):
            if raw and not is_encrypted(raw):
                sets[col] = encrypt_str(raw)
        if sets:
            assignments = ", ".join(f"{c} = :{c}" for c in sets)
            session.execute(
                sa_text(f"UPDATE accounts SET {assignments} WHERE id = :id"),
                {**sets, "id": row.id},
            )
            changed += 1
    if changed:
        session.commit()
    return changed


def backfill_files(data_dir: Path) -> int:
    base = data_dir / "browser_states"
    if not base.exists():
        return 0
    changed = 0
    for path in base.rglob("storage_state.json"):
        raw = path.read_bytes()
        if is_encrypted(raw):
            continue
        state = json.loads(raw.decode("utf-8"))
        write_state(path, state)
        changed += 1
    return changed


def main() -> None:
    with SessionLocal() as session:
        db_changed = backfill_db(session)
    file_changed = backfill_files(get_data_dir())
    print(f"encrypted {db_changed} account rows, {file_changed} storage_state files")


if __name__ == "__main__":
    main()
```

> 实施注意：确认 `get_data_dir` 的 import 来源（`server.app.db.session` 或 `server.app.modules.accounts.service`——以实际为准）。`row.api_credentials` 用具名访问需 SQLAlchemy `Row`；若版本不支持具名，改 `row[1]` / `row[2]`。

- [ ] **Step 4: 跑测试确认通过**

Run: `GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test python -m pytest server/tests/test_encrypt_secrets_backfill.py -q`
Expected: PASS（2 passed）

- [ ] **Step 5: 门禁 + 提交**

Run: `ruff check server/scripts/encrypt_secrets.py && ruff format --check server/scripts/encrypt_secrets.py && mypy server/scripts/encrypt_secrets.py`
Expected: 全过

```bash
git add server/scripts/encrypt_secrets.py server/tests/test_encrypt_secrets_backfill.py
git commit -m "feat(security): 幂等回填脚本 encrypt_secrets（DB 字段 + storage_state 文件）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: 文档与运维手册

**Files:**
- Modify: `CLAUDE.md`（PlatformDriver 小节 + Gotchas + 新增「凭据加密」段）
- Modify: `.env.example`（若不存在则在仓库根创建）

**Interfaces:** 无代码接口；纯文档。

- [ ] **Step 1: CLAUDE.md 加「凭据加密」说明** — 在 `## PlatformDriver` 小节末尾追加：

```markdown
驱动若需自存浏览器登录态，用 `accounts/secret_files.py` 的 `write_state(path, dict)` /
`read_state(path)`，**不要**裸调 `context.storage_state(path=...)`——前者会按 `GEO_SECRET_KEY`
透明加密。API 驱动把凭据存 `Account.api_credentials`（`EncryptedJSON` 列，自动加密），
新增其它敏感列时列类型用 `core/encrypted_types.EncryptedJSON` / `EncryptedText` 即得加密。
```

- [ ] **Step 2: CLAUDE.md Gotchas 加三条** — 在 `## Gotchas` 列表追加：

```markdown
- 敏感凭据静态加密走 `GEO_SECRET_KEY`（或 `GEO_SECRET_KEYS` 逗号分隔多钥轮换，首钥加密、全钥解密）。
  空 = NullCipher 透传（本地/测试零配置）。**丢密钥 = 丢 app_secret / 登录态**（需平台重置 / 重新登录）——
  密钥纳入 secret 管理，且**与 DB 备份分开存**。生成：`python -m server.scripts.gen_secret_key`。
- 生产启用加密的顺序固定：① `alembic upgrade head`（`0049` 把两列 JSON→TEXT）→ ② 部署带密钥的新代码 →
  ③ `python -m server.scripts.encrypt_secrets` 幂等回填。读路径全程认 `enc:v1:` 前缀、无前缀＝明文，
  故迁移期混存安全、零停机。`encrypt_secrets` 可重跑。
- `profile/`（Chromium 持久化目录，发布路径真正的 cookie 源）**不做 app 层加密**，交数据盘 / 卷级加密
  （LUKS / 云盘）。app 层只覆盖 `api_credentials` / `api_token_cache` / `storage_state.json` / 导出 ZIP。
  导出授权 ZIP 内是**明文** cookie（保便携），属敏感物，操作者自行保管。
```

- [ ] **Step 3: .env.example 加密钥占位** — 在 `.env.example`（无则创建）加：

```bash
# 敏感凭据静态加密（留空 = 不加密，本地开发可不设）。
# 生成：python -m server.scripts.gen_secret_key
GEO_SECRET_KEY=
# 多密钥轮换（逗号分隔，首钥加密、全钥解密）；设了优先于 GEO_SECRET_KEY
# GEO_SECRET_KEYS=
```

- [ ] **Step 4: 提交**

```bash
git add CLAUDE.md .env.example
git commit -m "docs(security): 凭据加密的密钥配置/部署顺序/丢钥代价 + PlatformDriver 约定

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: 全量回归 + 收尾

- [ ] **Step 1: 无密钥跑全量后端测试**（验证 NullCipher 透传不破坏任何现有用例）

Run: `GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test python -m pytest server/tests/ -q`
Expected: PASS（与改动前同等通过数 + 本计划新增用例）

- [ ] **Step 2: 设密钥跑加密相关用例**（验证加密路径）

Run:
```bash
GEO_SECRET_KEY=$(python -m server.scripts.gen_secret_key) \
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test \
python -m pytest server/tests/test_crypto.py server/tests/test_encrypted_column.py \
server/tests/test_secret_files.py server/tests/test_encrypt_secrets_backfill.py \
server/tests/test_accounts_import_export.py -q
```
Expected: PASS

- [ ] **Step 3: 全量门禁**

Run: `ruff check server/ && ruff format --check server/ && mypy server/app`
Expected: 全过

- [ ] **Step 4: 推分支 + 开 PR**（按团队流程；记忆：用 `git push origin <branch>` 不带 `-u`）

```bash
git push origin feat/secret-encryption
```
然后在 GitLab 开 PR。PR 描述附：部署顺序（迁移→代码→回填）、丢钥代价、与 `publish-network-retry` 的迁移协调（本迁移先落、对方 rebase）。

---

## Self-Review

**Spec coverage（逐节核对 spec → 任务映射）：**
- §4.1 加密核心 / 信封 / MultiFernet / NullCipher → Task 1 ✓
- §4.2 EncryptedJSON + 两列换类型 + 检索安全 → Task 2 + Task 4 ✓
- §4.3 secret_files + 6 处 I/O → Task 3 + Task 5 ✓
- §5.1 迁移（code-first / 列类型 / 幂等回填）→ Task 4（迁移）+ Task 6（回填）+ Task 7（部署顺序文档）✓
- §5.2 导出明文 / 导入重加密 → Task 5 Step 6-7 ✓
- §5.3 密钥配置 + gen_secret_key → Task 1 ✓
- §6 测试矩阵（5 个测试文件）→ Task 1/2/3/4/5/6 各自测试 ✓
- §7 文件清单 → 全覆盖（requirements Task 1、CLAUDE.md/.env Task 7）✓
- §8 风险（丢钥 / profile / ALTER / 幂等 / ZIP / 混存）→ Task 7 文档 + Task 6 幂等测试 ✓
- §9 迁移协调（本迁移先落）→ Task 4 迁移注释 + Task 8 PR 描述 ✓

**Placeholder scan：** Task 5 Step 1 的测试含 `...` 脚手架占位——**已显式标注**为「按 `test_accounts_import_export.py` 既有 helper 补全」并列出四条不可省断言；其余步骤均为可执行真代码。测试接口已对齐 `server/tests/utils.py`：`build_test_app(monkeypatch)` / `test_app.session_factory()`（session 上下文管理器）/ `test_app.client`（带 admin JWT），参照 `test_accounts_api_wechat.py`。

**Type consistency：** `read_state(Path)->dict` / `write_state(Path, dict)->None` 在 Task 3 定义、Task 5 一致使用；`encrypt_str/decrypt_str/encrypt_bytes/decrypt_bytes/is_encrypted` 在 Task 1 定义、Task 2/3/6 一致引用；`backfill_db(session)->int` / `backfill_files(Path)->int` 在 Task 6 定义并被其测试一致调用；`get_cipher`/`get_settings` 的 `cache_clear()` 在所有改 env 的测试中成对出现。
