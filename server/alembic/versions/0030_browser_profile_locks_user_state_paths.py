"""browser profile locks and user scoped account state paths

Revision ID: 0030
Revises: 0029
Create Date: 2026-05-26
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect


revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


LOCK_NAME = "geo_migration_0030_browser_state_paths"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)

    if "browser_profile_locks" not in inspector.get_table_names():
        op.create_table(
            "browser_profile_locks",
            sa.Column("profile_key", sa.String(length=255), nullable=False),
            sa.Column("owner_kind", sa.String(length=40), nullable=False),
            sa.Column("owner_id", sa.String(length=80), nullable=False),
            sa.Column("worker_id", sa.String(length=100), nullable=True),
            sa.Column("queue_reason", sa.String(length=500), nullable=True),
            sa.Column("acquired_at", sa.DateTime(), nullable=False),
            sa.Column("heartbeat_at", sa.DateTime(), nullable=False),
            sa.Column("lease_until", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("profile_key"),
        )
        op.create_index(op.f("ix_browser_profile_locks_worker_id"), "browser_profile_locks", ["worker_id"], unique=False)
        op.create_index(op.f("ix_browser_profile_locks_lease_until"), "browser_profile_locks", ["lease_until"], unique=False)

    with op.batch_alter_table("account_login_sessions", schema=None) as batch_op:
        cols = {c["name"] for c in inspector.get_columns("account_login_sessions")}
        if "queue_reason" not in cols:
            batch_op.add_column(sa.Column("queue_reason", sa.String(length=500), nullable=True))

    with op.batch_alter_table("browser_sessions", schema=None) as batch_op:
        cols = {c["name"] for c in inspector.get_columns("browser_sessions")}
        if "profile_key" not in cols:
            batch_op.add_column(sa.Column("profile_key", sa.String(length=255), nullable=True))

    with op.batch_alter_table("publish_records", schema=None) as batch_op:
        cols = {c["name"] for c in inspector.get_columns("publish_records")}
        if "queue_reason" not in cols:
            batch_op.add_column(sa.Column("queue_reason", sa.String(length=500), nullable=True))

    _migrate_account_state_paths()


def downgrade() -> None:
    with op.batch_alter_table("publish_records", schema=None) as batch_op:
        batch_op.drop_column("queue_reason")

    with op.batch_alter_table("browser_sessions", schema=None) as batch_op:
        batch_op.drop_column("profile_key")

    with op.batch_alter_table("account_login_sessions", schema=None) as batch_op:
        batch_op.drop_column("queue_reason")

    op.drop_index(op.f("ix_browser_profile_locks_lease_until"), table_name="browser_profile_locks")
    op.drop_index(op.f("ix_browser_profile_locks_worker_id"), table_name="browser_profile_locks")
    op.drop_table("browser_profile_locks")


def _migrate_account_state_paths() -> None:
    bind = op.get_bind()
    got_lock = bind.execute(sa.text("SELECT GET_LOCK(:name, 60)"), {"name": LOCK_NAME}).scalar()
    if got_lock != 1:
        raise RuntimeError(f"Could not acquire migration lock: {LOCK_NAME}")
    try:
        data_dir = _data_dir()
        rows = bind.execute(
            sa.text(
                """
                SELECT a.id, a.user_id, a.state_path, p.code AS platform_code
                FROM accounts a
                JOIN platforms p ON p.id = a.platform_id
                WHERE a.state_path LIKE 'browser_states/%/storage_state.json'
                   OR a.state_path LIKE 'browser_states/%/%/storage_state.json'
                """
            )
        ).mappings().all()

        for row in rows:
            old_rel = str(row["state_path"])
            parsed = _parse_legacy_state_path(old_rel)
            if parsed is None:
                continue
            platform_code, account_key = parsed
            new_rel = f"browser_states/users/{row['user_id']}/{platform_code}/{account_key}/storage_state.json"
            if old_rel == new_rel:
                continue

            old_dir = data_dir / "browser_states" / platform_code / account_key
            new_dir = data_dir / "browser_states" / "users" / str(row["user_id"]) / platform_code / account_key
            _copy_state_dir(old_dir, new_dir)
            bind.execute(
                sa.text("UPDATE accounts SET state_path = :state_path WHERE id = :id"),
                {"state_path": new_rel, "id": row["id"]},
            )
    finally:
        bind.execute(sa.text("SELECT RELEASE_LOCK(:name)"), {"name": LOCK_NAME})


def _data_dir() -> Path:
    configured = os.environ.get("GEO_DATA_DIR")
    if configured:
        return Path(configured)
    return Path("data")


def _parse_legacy_state_path(state_path: str) -> tuple[str, str] | None:
    parts = Path(state_path).parts
    try:
        idx = parts.index("browser_states")
    except ValueError:
        return None
    if len(parts) <= idx + 3:
        return None
    if parts[idx + 1] == "users":
        return None
    platform_code = parts[idx + 1]
    account_key = parts[idx + 2]
    return platform_code, account_key


def _copy_state_dir(old_dir: Path, new_dir: Path) -> None:
    if not old_dir.exists():
        new_dir.mkdir(parents=True, exist_ok=True)
        return
    new_dir.mkdir(parents=True, exist_ok=True)
    for child in old_dir.iterdir():
        dest = new_dir / child.name
        if child.is_dir():
            if dest.exists():
                continue
            shutil.copytree(child, dest)
        elif child.is_file() and not dest.exists():
            shutil.copy2(child, dest)
