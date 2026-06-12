"""从 GEO_SEED_USERS 环境变量种入用户。

期望格式为 JSON 数组：[{"username":"admin","password":"xxx","role":"admin"}, ...]
该脚本幂等：已存在的用户会被跳过。
"""

from __future__ import annotations

import json
import os

# 独立运行时必须导入全部模块的 models，否则 SQLAlchemy 配置 mapper 时
# 解析跨模块 relationship（如 Platform→Account）会因类名未注册而失败。
import server.app.modules.accounts.models  # noqa: F401,E402
import server.app.modules.ai_generation.models  # noqa: F401,E402
import server.app.modules.articles.models  # noqa: F401,E402
import server.app.modules.audit.models  # noqa: F401,E402
import server.app.modules.image_library.models  # noqa: F401,E402
import server.app.modules.prompt_templates.models  # noqa: F401,E402
import server.app.modules.skills.models  # noqa: F401,E402
import server.app.modules.tasks.models  # noqa: F401,E402
from server.app.db.session import SessionLocal
from server.app.modules.system.models import User  # noqa: E402


def main() -> None:
    raw = os.environ.get("GEO_SEED_USERS")

    if not raw:
        print("GEO_SEED_USERS not set, skipping seed")
        return

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        print("GEO_SEED_USERS is not valid JSON, skipping seed")
        return

    if not isinstance(entries, list):
        print("GEO_SEED_USERS is not a JSON array, skipping seed")
        return

    created: list[str] = []

    with SessionLocal() as session:
        for entry in entries:
            username = entry.get("username")
            password = entry.get("password")
            role = entry.get("role", "operator")

            if not username or not password:
                print(f"Skipping entry with missing username/password: {entry}")
                continue

            existing = session.query(User).filter(User.username == username).first()
            if existing is not None:
                print(f"User '{username}' already exists, skipping")
                continue

            user = User(username=username, role=role, must_change_password=True)
            user.set_password(password)
            session.add(user)
            created.append(username)
            print(f"Created user '{username}' with role '{role}'")

        if created:
            session.commit()
            print(f"Seeded {len(created)} users: {', '.join(created)}")
        else:
            print("No new users to create")


if __name__ == "__main__":
    main()
