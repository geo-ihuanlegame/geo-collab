# 账号敏感凭据静态加密（通用加密抽象层）

- 日期：2026-06-23
- 状态：设计已确认，待出实施计划
- 范围：后端（`server/app/core`、`accounts`、`tasks/drivers`、`scripts`、`alembic`）

## 1. 背景与问题

GEO 平台两类账号的存储现状：

- **GEO 平台用户密码**（`User.password_hash`）—— 已用 bcrypt 加盐哈希（[system/models.py](../../../server/app/modules/system/models.py)），单向不可逆，**本设计不动它**。
- **平台媒体账号凭据** —— 基本明文：
  - 公众号 `app_secret`：存 `Account.api_credentials` JSON 列，**DB 明文**。
  - `access_token`：存 `Account.api_token_cache` JSON 列，**DB 明文**。
  - 浏览器登录态：`storage_state.json`（**磁盘明文文件**）+ Chromium `profile/` 持久化目录（**磁盘明文目录**）。

现有的「保护」只是接口层回传脱敏（`app_secret_tail` 尾 4 位、删账号时 pop `app_secret`），**落库/落盘全是明文**。任何能读到 MySQL 数据或 `GEO_DATA_DIR` 的人（DBA、备份、拿到服务器的人）都能直接拿到 AppSecret 和登录 cookie。

全仓库当前无任何 `cryptography`/`Fernet`/`AES` 加密引用。

## 2. 目标与非目标

### 目标

- 提供一套**足够抽象、足够通用**的加密层，后续新平台新增各类敏感字段时能低成本接入。
- v1 用 app 层对称加密覆盖：**DB 字段（`app_secret`/`token`）+ `storage_state.json` + 导出 ZIP**。
- 生产**零停机平滑迁移**（已有明文生产数据）。
- 本地 / 测试 / CI **零配置不受影响**（无密钥即透传）。

### 非目标（明确排除）

- **不追求最强方案**：不上 KMS / Vault / HSM，不防「拿到服务器 root 的人」。
- **不做 `profile/` 目录的 app 层加密**：Chromium 运行时直接读写该目录（mmap 的 SQLite/LevelDB），app 层加密需「开浏览器前解密整目录→运行→关闭后再加密」，有明文窗口且改动大。**交给数据盘 / 卷级加密（LUKS / dm-crypt / 云盘加密）兜底**——对拖库 / 备份泄露同样有效，不动代码。
- **不动 GEO 用户密码哈希**（已用 bcrypt，本就该单向哈希、不是可逆加密）。
- **不建密钥轮换工具**：但信封格式与多密钥能力从第一天预留，零成本留口。

## 3. 威胁模型

**防 DB / 备份泄露**：密钥不在数据库里（走环境变量 / 密钥文件），拖库或拿到备份的人拿不到明文；应用进程持密钥、能解密。这是性价比最高的标准应用层对称加密。

**已确认的代价（须文档化）**：

- 对称加密 ⇒ **丢密钥 = 丢这些秘密**（`app_secret` 得去平台重置、cookie 得重新登录）。密钥须纳入现有 secret 管理 / 备份，且**与数据库备份分开存**（否则一起泄露＝白加密）。
- 导出授权 ZIP 内含明文 cookie，是敏感物，操作者自行保管（cookie 便携的固有属性，加密前后一致）。

## 4. 架构：方案 A —— 加密核心 + ORM 类型装饰器 + 文件助手

三层，核心只建一次。机制选择遵循「能透明的地方透明、不能的地方显式」：DB 列有 SQLAlchemy 钩子可挂 ⇒ 透明拦截；文件无钩子可挂 ⇒ 显式调用。

```
        ┌─────────────────────────────────────────────┐
        │  core/crypto.py  (纯函数加密核心，无状态)       │
        │  encrypt_str / decrypt_str / *_bytes /        │
        │  is_encrypted / get_cipher()                  │
        └───────────────┬─────────────────┬─────────────┘
                        │                 │
        ┌───────────────▼──────┐   ┌──────▼────────────────────┐
        │ EncryptedJSON /       │   │ accounts/secret_files.py  │
        │ EncryptedText         │   │ read_state / write_state  │
        │ (SQLAlchemy           │   │ (storage_state.json 显式) │
        │  TypeDecorator,透明)  │   │                           │
        └──────────────────────┘   └───────────────────────────┘
                 │ DB 列换类型即接入        │ 6 处 I/O 显式改调
```

### 4.1 加密核心 `server/app/core/crypto.py`

**信封格式（自描述、可演进）**

```
明文 app_secret  →  "enc:v1:gAAAAAB...<fernet_token>"
```

- 前缀 `enc:v1:` 是**唯一的「是否密文」判据**——明文 app_secret / cookie JSON 绝不以它开头，使「兼容读」100% 无歧义。
- `v1` 为格式版本号，将来换算法（如 AES-256-GCM）即 `v2`，老数据照旧能解。

**底层 `MultiFernet`**（`cryptography` 库；Fernet = AES-128-CBC + HMAC-SHA256 认证加密，IV/MAC/时间戳/版本均由库管）

- `GEO_SECRET_KEY` = 单个 Fernet 密钥（urlsafe-base64 的 32 字节）。
- `GEO_SECRET_KEYS` = 逗号分隔多密钥，**第一个加密、全部参与解密**（轮换天然支持：加新密钥到队首→跑回填重加密→删老密钥）。v1 不建轮换工具，但能力预留。

**对外 API（极小面）**

```python
def encrypt_str(plain: str) -> str          # → "enc:v1:..."
def decrypt_str(token: str) -> str          # 认前缀；无前缀=遗留明文，原样返回（兼容读）
def encrypt_bytes(plain: bytes) -> bytes
def decrypt_bytes(token: bytes) -> bytes
def is_encrypted(value: str | bytes) -> bool
def get_cipher() -> Cipher                   # @lru_cache 单例；测试改 env 后 cache_clear()
```

**无密钥 = NullCipher 透传**

- 未配 `GEO_SECRET_KEY` / `GEO_SECRET_KEYS` ⇒ 返回透传实现：`encrypt_str` / `decrypt_str` 原样返回。本地 / 测试 / CI 零配置不受影响（呼应仓库「AI Key 启动不校验、本地零配置」一贯风格）。
- prod 设密钥才真加密。启动时：检测到密钥打 INFO；无密钥打 WARNING（不致命）。
- **边界**：设了密钥→加密→又清掉密钥 ⇒ 无法再读（对称加密固有，文档化）。

### 4.2 适配器一：`EncryptedJSON` 类型装饰器（DB 字段，透明）

新增 `server/app/core/encrypted_types.py`：

```python
class EncryptedJSON(TypeDecorator):
    impl = Text
    cache_ok = True
    def process_bind_param(self, value, dialect):    # 写库前：dict → json → 加密
        return encrypt_str(json.dumps(value, ensure_ascii=False)) if value is not None else None
    def process_result_value(self, value, dialect):  # 读库后：解密 → json → dict
        return json.loads(decrypt_str(value)) if value else None

class EncryptedText(TypeDecorator):   # 预留：未来纯字符串敏感列
    impl = Text
    cache_ok = True
    # 同理：encrypt_str / decrypt_str
```

`accounts/models.py` 两列换类型，业务代码零改动：

```python
api_credentials: Mapped[dict | None] = mapped_column(
    MutableDict.as_mutable(EncryptedJSON()), nullable=True)   # 原 JSON
api_token_cache: Mapped[dict | None] = mapped_column(
    MutableDict.as_mutable(EncryptedJSON()), nullable=True)   # 原 JSON
```

- **整块 JSON 加密**，不做「只加密 app_secret 子字段」的精细手术——更通用、更简单。
- **检索安全性已核实**：无任何查询依赖从 JSON 读 `app_id`。去重 / 唯一性走独立的 `platform_user_id` 列（明文带索引，`service.py` 中 `Account.platform_user_id == app_id` 去重、`platform_user_id=app_id` 写入）；展示用 `creds.get("app_id")` 是读出来用、非查询条件。整块加密不破坏任何检索。
- `MutableDict.as_mutable(EncryptedJSON())` 照常工作：`process_result_value` 返回 dict → MutableDict 包好追踪变更。`verify_api_credentials` / `_active_access_token` / `delete_account`（pop `app_secret` 后重赋值触发 re-encrypt）/ `update_account_fields`（整体替换）均无需改动。
- **通用性**：未来任意平台新增敏感列，只需把列类型设为 `EncryptedJSON` / `EncryptedText`，拦截逻辑零重复。

### 4.3 适配器二：`secret_files.py`（storage_state 文件，显式）

新增 `server/app/modules/accounts/secret_files.py`：

```python
def read_state(path: Path) -> dict:
    """读文件→认 enc: 前缀→解密→json.loads；无前缀=遗留明文直接 load（兼容读）。"""

def write_state(path: Path, state: dict) -> None:
    """json.dumps→加密→落盘（带 enc:v1: 前缀）。"""
```

落点共 **6 处**（已全部定位）：

| 文件:行 | 现状 | 改成 |
|---------|------|------|
| `accounts/auth.py:1212` 读 | `new_context(storage_state=str(path))` | `new_context(storage_state=read_state(path))`（喂 dict） |
| `accounts/auth.py:1239` 写 | `context.storage_state(path=str(path))` | `write_state(path, context.storage_state())` |
| `accounts/login_broker.py:128` 写 | `_pw_storage_state` 内 `context.storage_state(path=...)` | 改 `write_state` |
| `tasks/drivers/toutiao.py:1072` 写 | `context.storage_state(path=str(payload.state_path))` | `write_state(...)` |
| `accounts/auth.py:1329` 读 | `json.loads(path.read_text())` 评估 cookie | `read_state(path)` |
| 导入 `accounts/auth.py:1402` 写 | `dest.write_bytes(archive.read(...))` | 见 §5.2 ZIP |

- Playwright 的 `storage_state` 既接受路径**也接受 dict**——加密版喂内存 dict，不明文落盘。
- login_broker 是 async；helper 是纯同步字节加解密，async 侧拿到 dict 后再调，不受影响。

> **不在 v1 范围**：`tasks/runner.py:346` 与 `login_broker.py:71` 的 `launch_persistent_context(user_data_dir=profile_dir)`——这是 `profile/` 活目录，交盘级加密（见 §2 非目标）。

## 5. 迁移与数据流

### 5.1 平滑迁移（零停机，code-first）

1. **先发代码**：`EncryptedJSON.process_result_value` 与 `read_state` 都认 `enc:v1:` 前缀——有前缀解密、无前缀当遗留明文原样返回。新代码上线后老明文照常读、新写入一律加密，无需先停机回填。
2. **再跑回填**：`python -m server.scripts.encrypt_secrets`（**幂等**，以前缀判断，跑几遍不双重加密）：
   - DB：遍历 accounts，`api_credentials` / `api_token_cache` 未带 `enc:` 前缀则加密回写，已加密跳过。
   - 文件：walk `GEO_DATA_DIR/browser_states/**/storage_state.json`，未加密的就地加密。
3. **列类型迁移**：一条 Alembic 把 `api_credentials` / `api_token_cache` 从 `JSON` 改 `TEXT`（accounts 小表，ALTER 便宜）。DDL 只改类型、不掺数据加密（加密交幂等脚本，职责分离、各自可回退）。
   - *备选*：若不想动 DDL，可让 `EncryptedJSON` 把密文存成 JSON 字符串标量（`"enc:v1:..."`）留在 JSON 列，零 DDL。默认走 TEXT（更直白：该列就是装密文的文本）。

### 5.2 导出 / 导入 ZIP

原则：**ZIP 内走明文（保便携，可导到别的实例），磁盘上永远密文**。

- **导出**（`auth.py:1296`）：`archive.write(原文件)` → `read_state(file)` 解密 → `archive.writestr(明文 JSON)`。
- **导入**（`auth.py:1402`）：`dest.write_bytes(ZIP 原字节)` → 解析后 `write_state(dest, state)`，**用本地密钥重新加密落盘**。`_assess_imported_status` 经 `read_state` 兼容读。
- 文档明记：授权 ZIP 含明文 cookie，敏感物自行保管。

### 5.3 密钥配置

```python
# config.py / Settings
secret_key: str = ""        # GEO_SECRET_KEY，单密钥
secret_keys: str = ""       # GEO_SECRET_KEYS，逗号分隔多密钥（轮换；优先于单密钥）
```

- `get_cipher()` 走 `@lru_cache`：有密钥建 `MultiFernet`、无密钥返回 NullCipher；测试改 env 后 `get_cipher.cache_clear()`。
- 生成密钥：`python -m server.scripts.gen_secret_key`（一行 `Fernet.generate_key()`）。
- `cryptography` 依赖：确认是否已被 Playwright 等间接带入，缺则补进 `requirements.txt`。

## 6. 测试

| 文件 | 覆盖 | 需 DB |
|------|------|-------|
| `test_crypto.py` | roundtrip / `enc:v1:` 前缀 / NullCipher 透传 / MultiFernet 轮换（k1 加密→k0 入队首仍可解）/ 遗留明文兼容读 / `is_encrypted` | 否（纯函数） |
| `test_secret_files.py` | write/read roundtrip / 遗留明文读 / 密文文件不含明文子串 | 否 |
| `test_encrypted_column.py` | `EncryptedJSON` roundtrip + 建账号读回相等 + 断言原始 DB cell 以 `enc:v1:` 开头（设密钥时） | 是 |
| `test_encrypt_secrets_backfill.py` | 脚本幂等（跑两遍不双加密）+ 明文 / 密文混存 | 是 |
| `test_accounts_import_export.py`（扩展现有） | 导出含明文可导入 / 导入后磁盘是密文 | 是 |

- 无密钥时 NullCipher 透传，**现有全部测试零改动照过**（本地 / CI 不需配密钥）。
- 纯函数测试（crypto / secret_files / TypeDecorator 单测）DB-less；账号集成 + 回填脚本走 `@pytest.mark.mysql` + `build_test_app`。

## 7. 文件清单

**新增**

- `server/app/core/crypto.py` —— 加密核心
- `server/app/core/encrypted_types.py` —— `EncryptedJSON` / `EncryptedText`
- `server/app/modules/accounts/secret_files.py` —— `read_state` / `write_state`
- `server/scripts/encrypt_secrets.py` —— 幂等回填脚本
- `server/scripts/gen_secret_key.py` —— 密钥生成
- `server/alembic/versions/00XX_encrypt_account_secret_columns.py` —— 列类型 JSON→TEXT
- `server/tests/test_crypto.py`、`test_secret_files.py`、`test_encrypted_column.py`、`test_encrypt_secrets_backfill.py`

**改动**

- `server/app/core/config.py` —— `secret_key` / `secret_keys` 配置
- `server/app/modules/accounts/models.py` —— 两列换 `EncryptedJSON`
- `server/app/modules/accounts/auth.py` —— storage_state 读写 + 导入导出（4 处）
- `server/app/modules/accounts/login_broker.py` —— `_pw_storage_state` 写（1 处）
- `server/app/modules/tasks/drivers/toutiao.py` —— 发布后回存（1 处）
- `server/tests/test_accounts_import_export.py` —— 扩展 ZIP 明文/密文断言
- `requirements.txt` —— 按需补 `cryptography`
- `CLAUDE.md` + `.env.example` —— 文档化密钥配置、丢钥代价、profile 目录交盘级加密

## 8. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 丢密钥 = 丢秘密 | 文档化；密钥纳入 secret 管理且与 DB 备份分离存 |
| `profile/` 目录仍明文 | 明确交盘 / 卷级加密；文档标注残留风险与对应运维手段 |
| 列类型 ALTER 锁表 | accounts 是小表；提供零 DDL 的 JSON 标量备选 |
| 回填脚本误重复加密 | 以 `enc:` 前缀判断，幂等可重跑 |
| 导出 ZIP 含明文 cookie | 文档化为敏感物；本属 cookie 便携固有属性 |
| 兼容读期混存明文/密文 | 前缀判据无歧义，读路径统一处理；新写一律加密 |
| 与并行迁移撞 head（多 head） | 见 §9 迁移协调 |

## 9. 与并行设计的迁移协调

本设计与同日并行的 `2026-06-23-publish-network-retry-design`（worktree `typed-strolling-emerson`）各新增一条 Alembic 迁移，且都基于当前 main 最新 head。**运行时与代码层零语义冲突**——本设计只碰 `accounts/*` + `core/crypto*`，对方只碰 `tasks/*` + `shared/resilience.py`；仅 `core/config.py`（双方各自往 Settings 追加设置）与 `tasks/drivers/toutiao.py`（本设计改 `:1072` storage_state 回存一行、对方改更靠前的导航/提交段）两处同文件不同段，自动合并干净。

唯一协调点是 Alembic 线性历史：

- **本迁移先落**（先合入 main，成为新 head）。
- 对方迁移**后落**：实施时把其 `down_revision` 重指到本迁移（`encrypt_account_secret_columns`），避免 `alembic upgrade head` 报 multiple heads。
- 取最新 head 的具体 revision 以实施期 `server/alembic/versions/` 实际为准（不写死版本号）。
