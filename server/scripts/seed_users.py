"""Seed users from the GEO_SEED_USERS environment variable.

Expects a JSON array: [{"username":"admin","password":"xxx","role":"admin"}, ...]
Idempotent: skips users that already exist.
"""

from __future__ import annotations

import json
import os

from server.app.db.session import SessionLocal
from server.app.modules.system.models import User


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
