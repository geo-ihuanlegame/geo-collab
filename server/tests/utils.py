import os
import shutil
import tempfile
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from server.app.core.config import get_settings
from server.app.db.base import Base


class TestApp:
    def __init__(self, client: TestClient, data_dir: Path, session_factory: sessionmaker[Session], engine: Engine):
        self.client = client
        self.data_dir = data_dir
        self.session_factory = session_factory
        self.engine = engine

    def cleanup(self) -> None:
        reset_test_database(self.engine)
        self.engine.dispose()
        shutil.rmtree(self.data_dir, ignore_errors=True)
        get_settings.cache_clear()


def get_test_database_url() -> str:
    url = os.environ.get("GEO_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set GEO_TEST_DATABASE_URL to a disposable MySQL database before running DB tests")
    parsed = urlparse(url)
    if parsed.scheme != "mysql+pymysql":
        raise RuntimeError("GEO_TEST_DATABASE_URL must use mysql+pymysql://")
    database_name = parsed.path.lstrip("/")
    if "test" not in database_name.lower() and os.environ.get("GEO_ALLOW_NON_TEST_DATABASE_FOR_TESTS") != "1":
        raise RuntimeError("Refusing to run tests unless the MySQL database name contains 'test'")
    return url


def build_test_engine() -> Engine:
    engine = create_engine(
        get_test_database_url(),
        pool_pre_ping=True,
        connect_args={"init_command": "SET SESSION time_zone='+00:00'"},
    )
    reset_test_database(engine)
    return engine


def reset_test_database(engine: Engine, *, create_schema: bool = True) -> None:
    import server.app.modules.system.models            # noqa: F401
    import server.app.modules.accounts.models          # noqa: F401
    import server.app.modules.articles.models          # noqa: F401
    import server.app.modules.tasks.models             # noqa: F401
    import server.app.modules.ai_generation.models     # noqa: F401
    import server.app.modules.image_library.models     # noqa: F401
    import server.app.modules.skills.models            # noqa: F401
    import server.app.modules.prompt_templates.models  # noqa: F401
    import server.app.modules.audit.models             # noqa: F401

    with engine.connect() as conn:
        conn.execute(sa.text("SET FOREIGN_KEY_CHECKS=0"))
        try:
            Base.metadata.drop_all(bind=conn)
            conn.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
            if create_schema:
                Base.metadata.create_all(bind=conn)
                conn.execute(
                    sa.text(
                        "ALTER TABLE articles ADD FULLTEXT INDEX ft_articles "
                        "(title, author, plain_text) WITH PARSER ngram"
                    )
                )
        finally:
            conn.execute(sa.text("SET FOREIGN_KEY_CHECKS=1"))
        conn.commit()


def build_test_app(monkeypatch) -> TestApp:
    test_database_url = get_test_database_url()
    data_dir = Path(tempfile.gettempdir()) / "geo-test-data" / uuid.uuid4().hex
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GEO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("GEO_DATABASE_URL", test_database_url)
    monkeypatch.setenv("GEO_JWT_SECRET", "test-secret")
    get_settings.cache_clear()

    from server.app.modules.tasks import executor as _tasks_mod
    _tasks_mod._task_locks.clear()
    _tasks_mod._account_locks.clear()
    _tasks_mod._account_locks_lock = threading.Lock()
    _tasks_mod._task_cancel.clear()

    from server.app.modules.accounts import browser as _bs_mod
    _bs_mod._reset_globals()

    from server.app.core import security as _security_mod
    _security_mod._reset_user_cache()

    engine = build_test_engine()
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db: Session = TestingSessionLocal()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    from server.app.db.session import get_db
    from server.app.main import create_app

    monkeypatch.setattr("server.app.modules.tasks.router.bg_session_factory", TestingSessionLocal)
    monkeypatch.setattr("server.app.db.session.SessionLocal", TestingSessionLocal)

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)

    from server.app.core.security import create_access_token

    with TestingSessionLocal() as db:
        from server.app.modules.system.models import User

        user = User(
            username="testadmin",
            role="admin",
            is_active=True,
            must_change_password=False,
        )
        user.set_password("testadmin")
        db.add(user)
        db.commit()
        db.refresh(user)
        token = create_access_token(user.id, user.role)
        client.cookies["access_token"] = token

    return TestApp(client=client, data_dir=data_dir, session_factory=TestingSessionLocal, engine=engine)
