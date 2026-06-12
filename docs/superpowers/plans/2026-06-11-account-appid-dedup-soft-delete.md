# 账号删除释放 app_id 占位 + 全局唯一 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除 API 账号（微信公众号）时释放其 app_id 身份槽位，使同一 app_id 可重新登记，同时把 app_id 查重收紧为全平台全局唯一，保留发布历史不动。

**Architecture:** 软删时把 `platform_user_id` 置空（=不占身份槽）、清 token、抹密钥但留 app_id；唯一约束从 `(user_id, platform_id, platform_user_id)` 改为全局 `(platform_id, platform_user_id)`；查重改为全局 + 仅活账号；一支 alembic 迁移清理存量死行并切换约束。不做物理删行，`PublishRecord` 外键不动。

**Tech Stack:** FastAPI + SQLAlchemy + Alembic（MySQL only）；pytest（`@pytest.mark.mysql`，需 `GEO_TEST_DATABASE_URL`）。

设计文档：`docs/superpowers/specs/2026-06-11-account-appid-dedup-soft-delete-design.md`

**前置：运行测试的环境**（见项目记忆）。conda activate 在工具 shell 里可能不生效；用环境内 python 全路径跑 pytest，并设 `GEO_TEST_DATABASE_URL`（库名必须含 `test`）。示例：
```bash
GEO_TEST_DATABASE_URL=mysql+pymysql://geo_user:password@127.0.0.1:3306/geo_test \
  python -m pytest server/tests/test_accounts_api_wechat.py -q
```

---

## File Structure

- `server/app/modules/accounts/service.py` — 改 `delete_account`（释放槽位）、`create_api_account` / `_ensure_app_id_available`（全局 + 仅活账号查重 + IntegrityError 兜底）。
- `server/app/modules/accounts/models.py` — `Account.__table_args__` 里 `uq_accounts_platform_user` 改为 `(platform_id, platform_user_id)`（驱动测试 schema）。
- `server/app/modules/accounts/auth.py` — `_accounts_for_export` 排除软删行。
- `server/alembic/versions/0045_accounts_global_appid_unique.py` — 新迁移：清理存量死行 + 冲突探测 + 约束切换（驱动生产 schema）。
- `server/tests/test_accounts_api_wechat.py` — 扩/翻转账号行为测试。
- `server/tests/test_accounts_appid_migration.py` — 新建，迁移专项测试。

---

## Task 1: 删除时释放身份槽位（`delete_account`）

**Files:**
- Modify: `server/app/modules/accounts/service.py:312-334`（`delete_account`）
- Test: `server/tests/test_accounts_api_wechat.py`、`server/tests/test_delete_guards.py`

- [ ] **Step 1: Write the failing tests**

(a) 在 `server/tests/test_accounts_api_wechat.py` 末尾追加：

```python
def test_delete_wechat_account_frees_identity_slot(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        account_id = test_app.client.post("/api/accounts", json=_create_payload()).json()["id"]
        monkeypatch.setattr(
            "server.app.modules.accounts.service.wechat_fetch_access_token",
            lambda app_id, app_secret, client=None: ("tok-1", 7200),
        )
        assert (
            test_app.client.post(f"/api/accounts/{account_id}/verify-credentials").status_code
            == 200
        )

        assert test_app.client.delete(f"/api/accounts/{account_id}").status_code == 204

        from server.app.modules.accounts.models import Account

        with test_app.session_factory() as db:
            acc = db.get(Account, account_id)  # db.get 不过滤 is_deleted，能取到死行
            assert acc.is_deleted is True
            assert acc.deleted_at is not None
            assert acc.platform_user_id is None
            assert acc.api_token_cache is None
            creds = acc.api_credentials or {}
            assert "app_secret" not in creds          # 密钥已抹除
            assert creds.get("app_id") == "wx8f2a91c0d3e5b6"  # app_id 保留供审计
    finally:
        test_app.cleanup()
```

(b) 在 `server/tests/test_delete_guards.py` 末尾追加（复用文件内既有 helper `_create_article` / `_create_account` / `_create_task_and_record`，已 import `Account` / `PublishRecord`）——验证已完结记录不阻止删除、且发布历史在软删后仍指向账号行：

```python
def test_account_delete_preserves_publish_history(monkeypatch):
    test_app = build_test_app(monkeypatch)
    client = test_app.client
    try:
        article_id = _create_article(client)
        account_id = _create_account(test_app, "acc-history", "Acc")
        record_id = _create_task_and_record(test_app, article_id, account_id, "succeeded")

        # 无活跃记录 → 软删放行（默认 client 是 admin，删除端点要求 admin）
        assert client.delete(f"/api/accounts/{account_id}").status_code == 204

        with test_app.session_factory() as db:
            acc = db.get(Account, account_id)
            assert acc.is_deleted is True
            rec = db.get(PublishRecord, record_id)
            assert rec is not None
            assert rec.account_id == account_id  # 历史仍指向账号行，未被破坏
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `GEO_TEST_DATABASE_URL=... python -m pytest server/tests/test_accounts_api_wechat.py::test_delete_wechat_account_frees_identity_slot server/tests/test_delete_guards.py::test_account_delete_preserves_publish_history -q`
Expected: `frees_identity_slot` FAIL —— 当前 `delete_account` 不清 `platform_user_id` / token / secret。`preserves_publish_history` 可能已 PASS（历史本就不被触碰）；保留它作为回归护栏，确认本任务改动不破坏历史。

- [ ] **Step 3: Write minimal implementation**

把 `server/app/modules/accounts/service.py` 的 `delete_account` 替换为：

```python
def delete_account(db: Session, account: Account) -> None:
    """软删账号并释放身份槽位；仍有未完成发布记录时抛 ClientError 拒绝删除。

    释放槽位＝置空 platform_user_id（全局唯一约束据此放行同一 app_id 重新登记）、
    清 api_token_cache、抹除 api_credentials.app_secret（保留 app_id 供审计）。
    发布历史（PublishRecord.account_id）不动。
    """
    account_id = account.id

    active = (
        db.execute(
            select(PublishRecord.id).where(
                PublishRecord.account_id == account_id,
                PublishRecord.status.in_(
                    ["pending", "running", "waiting_manual_publish", "waiting_user_input"]
                ),
            )
        )
        .scalars()
        .all()
    )
    if active:
        raise ClientError("存在未完成发布记录，无法删除账号")

    account.is_deleted = True
    account.deleted_at = utcnow()
    account.platform_user_id = None
    account.api_token_cache = None
    if account.api_credentials:
        creds = dict(account.api_credentials)
        creds.pop("app_secret", None)
        account.api_credentials = creds or None
    account.updated_at = utcnow()
    db.flush()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `GEO_TEST_DATABASE_URL=... python -m pytest server/tests/test_accounts_api_wechat.py::test_delete_wechat_account_frees_identity_slot server/tests/test_delete_guards.py -q`
Expected: PASS（新用例 + 既有删除守卫用例全绿——确认重写 `delete_account` 未破坏「未完成记录阻止删除」守卫）。

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/accounts/service.py server/tests/test_accounts_api_wechat.py server/tests/test_delete_guards.py
git commit -m "feat(accounts): 删除账号时释放 app_id 身份槽位(置空 platform_user_id+抹密钥)"
```

---

## Task 2: 全局 + 仅活账号查重（models + service）

**Files:**
- Modify: `server/app/modules/accounts/models.py:33-38`（`uq_accounts_platform_user`）
- Modify: `server/app/modules/accounts/service.py:8-34`（顶部 import 加 `IntegrityError`）
- Modify: `server/app/modules/accounts/service.py:175-228`（`create_api_account` + `_ensure_app_id_available`）
- Test: `server/tests/test_accounts_api_wechat.py`

- [ ] **Step 1: Write/flip the failing tests**

(a) 把现有的 `test_create_soft_deleted_duplicate_app_id_conflict`（约 `server/tests/test_accounts_api_wechat.py:100-110`）**整体替换**为重新登记成功的用例：

```python
def test_recreate_after_delete_reuses_app_id(monkeypatch):
    """删了同一 app_id 能重新登记（旧行为是 409，现因软删释放槽位而放行）。"""
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        account_id = test_app.client.post("/api/accounts", json=_create_payload()).json()["id"]
        assert test_app.client.delete(f"/api/accounts/{account_id}").status_code == 204

        resp = test_app.client.post("/api/accounts", json=_create_payload())
        assert resp.status_code == 200, resp.text
        assert resp.json()["app_id"] == "wx8f2a91c0d3e5b6"
        assert resp.json()["id"] != account_id
    finally:
        test_app.cleanup()
```

(b) 追加跨用户全局唯一用例：

```python
def test_app_id_globally_unique_across_users(monkeypatch):
    """一个 app_id 全平台只能活一份：A 用户登记后，B 用户登记同一 app_id 应 409。"""
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        assert test_app.client.post("/api/accounts", json=_create_payload()).status_code == 200

        from server.tests.utils import create_extra_user

        _uid, other_client = create_extra_user(test_app, "operator2")
        resp = other_client.post("/api/accounts", json=_create_payload())
        assert resp.status_code == 409, resp.text
    finally:
        test_app.cleanup()
```

（保留现有 `test_create_duplicate_app_id_conflict`：同用户两个活账号仍应 409。）

- [ ] **Step 2: Run tests to verify they fail**

Run: `GEO_TEST_DATABASE_URL=... python -m pytest server/tests/test_accounts_api_wechat.py -q -k "recreate_after_delete or globally_unique"`
Expected: FAIL —— `recreate_after_delete` 因旧 DB 约束（含 user_id 且算软删行）在 flush 抛 IntegrityError→500/409 失败；`globally_unique` 因查重仍带 user_id 而第二个用户被放行（200）。

- [ ] **Step 3: Write minimal implementation**

(3a) `server/app/modules/accounts/models.py` 把唯一约束的列去掉 `user_id`：

```python
    __table_args__ = (
        UniqueConstraint(
            "platform_id", "platform_user_id", name="uq_accounts_platform_user"
        ),
        CheckConstraint("status in ('valid', 'expired', 'unknown')", name="ck_accounts_status"),
    )
```

(3b) `server/app/modules/accounts/service.py` 顶部 import 段加一行（与现有 `from sqlalchemy import select` 相邻）：

```python
from sqlalchemy.exc import IntegrityError
```

(3c) `create_api_account` 里的查重块（去 `user_id`、加 `is_deleted` 过滤、改文案）替换为：

```python
    app_id = payload.api_credentials.app_id
    duplicate = db.execute(
        select(Account.id).where(
            Account.platform_id == platform.id,
            Account.platform_user_id == app_id,
            Account.is_deleted == False,  # noqa: E712
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ConflictError(f"该 AppID 已被登记（全平台唯一）: {app_id}")
```

并把 `create_api_account` 末尾的 `db.add(account)` / `db.flush()` 段替换为带并发兜底的版本：

```python
    db.add(account)
    try:
        db.flush()
    except IntegrityError as exc:  # 并发抢注同一 app_id：DB 全局唯一约束兜底
        db.rollback()
        raise ConflictError(f"该 AppID 已被登记（全平台唯一）: {app_id}") from exc
    return get_account(db, account.id) or account
```

(3d) `_ensure_app_id_available` 改为全局 + 仅活账号：

```python
def _ensure_app_id_available(db: Session, account: Account, app_id: str) -> None:
    duplicate = db.execute(
        select(Account.id).where(
            Account.platform_id == account.platform_id,
            Account.platform_user_id == app_id,
            Account.is_deleted == False,  # noqa: E712
            Account.id != account.id,
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ConflictError(f"该 AppID 已被登记（全平台唯一）: {app_id}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `GEO_TEST_DATABASE_URL=... python -m pytest server/tests/test_accounts_api_wechat.py -q`
Expected: PASS（含翻转后的 `recreate_after_delete`、新 `globally_unique`、保留的 `test_create_duplicate_app_id_conflict`，以及 Task 1 用例）。

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/accounts/models.py server/app/modules/accounts/service.py server/tests/test_accounts_api_wechat.py
git commit -m "feat(accounts): app_id 查重改全局唯一+仅活账号，删后可重登同一 app_id"
```

---

## Task 3: Alembic 迁移（清理死行 + 冲突探测 + 约束切换）

**Files:**
- Create: `server/alembic/versions/0045_accounts_global_appid_unique.py`
- Create (test): `server/tests/test_accounts_appid_migration.py`

- [ ] **Step 1: Write the failing migration test**

新建 `server/tests/test_accounts_appid_migration.py`：

```python
from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, text

from alembic import command
from server.app.core.config import get_settings
from server.tests.utils import get_test_database_url, reset_test_database


def _data_dir() -> Path:
    return Path(tempfile.gettempdir()) / "geo-test-data" / uuid.uuid4().hex


def _seed_user(conn, username: str) -> int:
    conn.execute(
        text(
            "INSERT INTO users (username, password_hash, role, is_active, "
            "must_change_password, solo_mode, created_at) "
            "VALUES (:u, 'x', 'admin', 1, 0, 0, NOW())"
        ),
        {"u": username},
    )
    return conn.execute(text("SELECT id FROM users WHERE username=:u"), {"u": username}).scalar()


def _wechat_platform_id(conn) -> int:
    return conn.execute(text("SELECT id FROM platforms WHERE code='wechat_mp'")).scalar()


@pytest.mark.mysql
def test_migration_0045_cleans_dead_rows_and_swaps_constraint(monkeypatch):
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(get_test_database_url(), pool_pre_ping=True)
    reset_test_database(engine, create_schema=False)
    monkeypatch.setenv("GEO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("GEO_DATABASE_URL", get_test_database_url())
    get_settings.cache_clear()

    try:
        cfg = AlembicConfig("alembic.ini")
        command.upgrade(cfg, "0044")

        with engine.begin() as conn:
            uid = _seed_user(conn, "mig-u1")
            pid = _wechat_platform_id(conn)
            conn.execute(
                text(
                    "INSERT INTO accounts (user_id, platform_id, display_name, platform_user_id, "
                    "status, is_deleted, deleted_at, api_credentials, api_token_cache, "
                    "distribution_enabled, created_at, updated_at) VALUES "
                    "(:uid, :pid, 'dead', 'wxDEAD', 'unknown', 1, NOW(), :creds, :tok, 1, "
                    "NOW(), NOW())"
                ),
                {
                    "uid": uid,
                    "pid": pid,
                    "creds": json.dumps({"app_id": "wxDEAD", "app_secret": "sek"}),
                    "tok": json.dumps({"access_token": "t", "expires_at": 1}),
                },
            )

        command.upgrade(cfg, "0045")

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT platform_user_id, api_token_cache, api_credentials "
                    "FROM accounts WHERE display_name='dead'"
                )
            ).mappings().one()
            assert row["platform_user_id"] is None
            assert row["api_token_cache"] is None
            creds = json.loads(row["api_credentials"])
            assert "app_secret" not in creds
            assert creds["app_id"] == "wxDEAD"

            idx = conn.execute(
                text("SHOW INDEX FROM accounts WHERE Key_name='uq_accounts_platform_user'")
            ).mappings().all()
            cols = {r["Column_name"] for r in idx}
            assert cols == {"platform_id", "platform_user_id"}
    finally:
        reset_test_database(engine, create_schema=False)
        engine.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)
        get_settings.cache_clear()


@pytest.mark.mysql
def test_migration_0045_aborts_on_live_cross_user_dup(monkeypatch):
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(get_test_database_url(), pool_pre_ping=True)
    reset_test_database(engine, create_schema=False)
    monkeypatch.setenv("GEO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("GEO_DATABASE_URL", get_test_database_url())
    get_settings.cache_clear()

    try:
        cfg = AlembicConfig("alembic.ini")
        command.upgrade(cfg, "0044")

        with engine.begin() as conn:
            pid = _wechat_platform_id(conn)
            for uname in ("mig-a", "mig-b"):
                uid = _seed_user(conn, uname)
                conn.execute(
                    text(
                        "INSERT INTO accounts (user_id, platform_id, display_name, "
                        "platform_user_id, status, is_deleted, distribution_enabled, "
                        "created_at, updated_at) VALUES "
                        "(:uid, :pid, :nm, 'wxLIVE', 'unknown', 0, 1, NOW(), NOW())"
                    ),
                    {"uid": uid, "pid": pid, "nm": uname},
                )

        with pytest.raises(Exception) as excinfo:
            command.upgrade(cfg, "0045")
        assert "wxLIVE" in str(excinfo.value) or "重复" in str(excinfo.value)
    finally:
        reset_test_database(engine, create_schema=False)
        engine.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)
        get_settings.cache_clear()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `GEO_TEST_DATABASE_URL=... python -m pytest server/tests/test_accounts_appid_migration.py -q`
Expected: FAIL —— revision `0045` 不存在，`command.upgrade(cfg, "0045")` 报 “Can't locate revision '0045'”。

- [ ] **Step 3: Write the migration**

新建 `server/alembic/versions/0045_accounts_global_appid_unique.py`：

```python
"""账号 app_id 查重改全局唯一：清理存量软删死行 + 唯一约束去掉 user_id

修订 ID: 0045
上一修订: 0044
创建日期: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1) 清理存量死行：释放占位 + 抹密钥（与新 delete_account 行为对齐）
    conn.execute(
        sa.text(
            "UPDATE accounts "
            "SET platform_user_id = NULL, "
            "    api_token_cache = NULL, "
            "    api_credentials = JSON_REMOVE(api_credentials, '$.app_secret') "
            "WHERE is_deleted = 1"
        )
    )

    # 2) 冲突探测：活账号里若已存在跨用户同 (platform_id, app_id)，无法建全局唯一约束 → 中止
    dupes = conn.execute(
        sa.text(
            "SELECT platform_id, platform_user_id, GROUP_CONCAT(id) AS ids "
            "FROM accounts "
            "WHERE is_deleted = 0 AND platform_user_id IS NOT NULL "
            "GROUP BY platform_id, platform_user_id "
            "HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if dupes:
        detail = "; ".join(
            f"platform_id={r[0]} app_id={r[1]} 重复行 id=[{r[2]}]" for r in dupes
        )
        raise RuntimeError(
            "迁移中止：存在跨用户重复的活账号 app_id，无法切换为全局唯一，请先人工合并/删除：" + detail
        )

    # 3) 切换唯一约束：(user_id, platform_id, platform_user_id) → (platform_id, platform_user_id)
    op.drop_constraint("uq_accounts_platform_user", "accounts", type_="unique")
    op.create_unique_constraint(
        "uq_accounts_platform_user", "accounts", ["platform_id", "platform_user_id"]
    )


def downgrade() -> None:
    # 注意：死行 platform_user_id / app_secret 的清理不可逆，仅恢复约束形状。
    op.drop_constraint("uq_accounts_platform_user", "accounts", type_="unique")
    op.create_unique_constraint(
        "uq_accounts_platform_user",
        "accounts",
        ["user_id", "platform_id", "platform_user_id"],
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `GEO_TEST_DATABASE_URL=... python -m pytest server/tests/test_accounts_appid_migration.py -q`
Expected: PASS（死行被清、约束变为两列；跨用户活重复时迁移抛错且消息含 `wxLIVE`）。

同时跑一遍空库到 head 的既有迁移用例，确认链路不断：
Run: `GEO_TEST_DATABASE_URL=... python -m pytest server/tests/test_fts_and_migrations.py::test_alembic_upgrade_from_empty_mysql_to_head -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/alembic/versions/0045_accounts_global_appid_unique.py server/tests/test_accounts_appid_migration.py
git commit -m "feat(accounts): 迁移0045 清理软删死行+app_id 唯一约束改全局"
```

---

## Task 4: 导出排除软删账号

**Files:**
- Modify: `server/app/modules/accounts/auth.py:1164-1181`（`_accounts_for_export`）
- Test: `server/tests/test_accounts_api_wechat.py`

- [ ] **Step 1: Write the failing test**

在 `server/tests/test_accounts_api_wechat.py` 追加：

```python
def test_export_excludes_soft_deleted_accounts(monkeypatch):
    test_app = build_test_app(monkeypatch)
    try:
        _ensure_wechat_platform(test_app)
        account_id = test_app.client.post("/api/accounts", json=_create_payload()).json()["id"]
        assert test_app.client.delete(f"/api/accounts/{account_id}").status_code == 204

        resp = test_app.client.post("/api/accounts/export", json={})
        assert resp.status_code == 200, resp.text
        with zipfile.ZipFile(BytesIO(resp.content)) as archive:
            prefix = f"accounts/wechat_mp-{account_id}/"
            assert not any(name.startswith(prefix) for name in archive.namelist())
    finally:
        test_app.cleanup()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `GEO_TEST_DATABASE_URL=... python -m pytest server/tests/test_accounts_api_wechat.py::test_export_excludes_soft_deleted_accounts -q`
Expected: FAIL —— 现 `_accounts_for_export` 不过滤 `is_deleted`，死行仍被打进 ZIP。

- [ ] **Step 3: Write minimal implementation**

把 `server/app/modules/accounts/auth.py` 的 `_accounts_for_export` 查询加上软删过滤：

```python
def _accounts_for_export(db: Session, account_ids: list[int] | None) -> list[Account]:
    from sqlalchemy.orm import selectinload

    stmt = (
        select(Account)
        .where(Account.is_deleted == False)  # noqa: E712
        .options(selectinload(Account.platform))
    )
    if account_ids:
        unique_ids = sorted(set(account_ids))
        stmt = stmt.where(Account.id.in_(unique_ids))
    else:
        unique_ids = []
    accounts = list(db.execute(stmt.order_by(Account.id.asc())).scalars().all())
    if unique_ids:
        found_ids = {account.id for account in accounts}
        missing_ids = [account_id for account_id in unique_ids if account_id not in found_ids]
        if missing_ids:
            raise ClientError(
                f"Accounts not found: {', '.join(str(account_id) for account_id in missing_ids)}"
            )
    return accounts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `GEO_TEST_DATABASE_URL=... python -m pytest server/tests/test_accounts_api_wechat.py -q`
Expected: PASS（新用例通过；既有 `test_export_all_skips_missing_browser_state_for_api_account` 仍通过——它导出的是活账号）。

- [ ] **Step 5: Commit**

```bash
git add server/app/modules/accounts/auth.py server/tests/test_accounts_api_wechat.py
git commit -m "fix(accounts): 授权包导出排除软删账号"
```

---

## Task 5: 全量校验门禁

**Files:** 无（仅运行检查）

- [ ] **Step 1: 后端 lint / format / 类型**

Run:
```bash
ruff check server/app/modules/accounts server/alembic/versions/0045_accounts_global_appid_unique.py server/tests/test_accounts_api_wechat.py server/tests/test_accounts_appid_migration.py
ruff format --check server/app/modules/accounts server/tests/test_accounts_appid_migration.py
mypy server/app/modules/accounts
```
Expected: 全部通过（mypy 宽松；新代码无类型错误）。

- [ ] **Step 2: 账号相关测试整体回归**

Run: `GEO_TEST_DATABASE_URL=... python -m pytest server/tests/test_accounts_api_wechat.py server/tests/test_delete_guards.py server/tests/test_accounts_appid_migration.py server/tests/test_fts_and_migrations.py -q`
Expected: PASS

- [ ] **Step 3: （可选）更广回归**

Run: `GEO_TEST_DATABASE_URL=... python -m pytest server/tests/test_models.py server/tests/test_articles_published_count.py -q`
Expected: PASS —— 验证唯一约束去掉 `user_id` 未波及创建多账号的既有用例。

- [ ] **Step 4: Commit（如有 lint/format 自动修复）**

```bash
git add -A
git commit -m "chore(accounts): 通过 lint/format/mypy 门禁"
```

---

## 实现注意（贯穿全程）

- **PR 描述必须点明语义变更**：app_id 查重 per-user → 全平台全局唯一；app_id 要转移需原持有者先删。
- **不要碰** `PublishRecord` / `PublishTaskAccount` 的外键——本方案有意保留发布历史。
- 迁移的「冲突探测中止」是安全阀：生产若真有跨用户重复活账号，迁移会失败并打印重复行，需人工裁决后再升级——这是设计预期，不是 bug。
- 前端删除确认文案（`AccountsWorkspace.tsx:247`「删除后将清除其授权信息，需重新授权才能恢复自动发文」）对软删语义已准确，**无需改动**。
