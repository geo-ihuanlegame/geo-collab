"""幂等回填：把明文 api_credentials / api_token_cache（DB）+ storage_state.json（文件）
就地加密。以 enc:v1: 前缀判断，可重跑。

用法（先确保 GEO_SECRET_KEY 已设、迁移已 upgrade head、新代码已部署）：
    python -m server.scripts.encrypt_secrets
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

# 独立运行需导入全部 models 触发 mapper 配置（同 seed_users.py）
import server.app.modules.accounts.models  # noqa: F401,E402
import server.app.modules.ai_generation.models  # noqa: F401,E402
import server.app.modules.articles.models  # noqa: F401,E402
import server.app.modules.audit.models  # noqa: F401,E402
import server.app.modules.image_library.models  # noqa: F401,E402
import server.app.modules.prompt_templates.models  # noqa: F401,E402
import server.app.modules.skills.models  # noqa: F401,E402
import server.app.modules.tasks.models  # noqa: F401,E402
from server.app.core.crypto import encrypt_str, is_encrypted
from server.app.core.paths import get_data_dir
from server.app.modules.accounts.secret_files import write_state

# NOTE: server.app.db.session 在 import 时即建引擎、需 DB 配置——放到 main() 里惰性导入，
# 否则 pytest collection 期（无 GEO_DATABASE_URL）import 本模块会 RuntimeError 拖垮整个 shard。

_COLUMNS = ("api_credentials", "api_token_cache")


def backfill_db(session: Session) -> int:
    rows = session.execute(
        sa_text("SELECT id, api_credentials, api_token_cache FROM accounts")
    ).all()
    changed = 0
    for row in rows:
        sets: dict[str, str] = {}
        cred_val = row.api_credentials
        cache_val = row.api_token_cache
        for col, raw in zip(_COLUMNS, (cred_val, cache_val), strict=False):
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
    from server.app.db.session import SessionLocal

    with SessionLocal() as session:
        db_changed = backfill_db(session)
    file_changed = backfill_files(get_data_dir())
    print(f"encrypted {db_changed} account rows, {file_changed} storage_state files")


if __name__ == "__main__":
    main()
