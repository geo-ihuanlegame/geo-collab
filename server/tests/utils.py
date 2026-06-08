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

# 共享 schema 缓存：首个 build_test_app 建一次表，其后测试间用 TRUNCATE 清数据，
# 不再每个测试都 DROP+CREATE 全部表（慢）。任何全量重置 / 裸 DDL 改 schema 都会把它
# 标记失效（reset_test_database / invalidate_test_schema），下个 build_test_app 会重建。
_schema_ready = False


class TestApp:
    def __init__(
        self,
        client: TestClient,
        data_dir: Path,
        session_factory: sessionmaker[Session],
        engine: Engine,
    ):
        self.client = client
        self.data_dir = data_dir
        self.session_factory = session_factory
        self.engine = engine

    def cleanup(self) -> None:
        # 不再 reset 数据库：下个 build_test_app 会按需 TRUNCATE / 重建，省掉一次全量 DDL。
        # schema 持久在库里、不随 engine 释放而消失。
        self.engine.dispose()
        shutil.rmtree(self.data_dir, ignore_errors=True)
        get_settings.cache_clear()


def get_test_database_url() -> str:
    url = os.environ.get("GEO_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "Set GEO_TEST_DATABASE_URL to a disposable MySQL database before running DB tests"
        )
    parsed = urlparse(url)
    if parsed.scheme != "mysql+pymysql":
        raise RuntimeError("GEO_TEST_DATABASE_URL must use mysql+pymysql://")
    database_name = parsed.path.lstrip("/")
    if (
        "test" not in database_name.lower()
        and os.environ.get("GEO_ALLOW_NON_TEST_DATABASE_FOR_TESTS") != "1"
    ):
        raise RuntimeError("Refusing to run tests unless the MySQL database name contains 'test'")
    return url


def _model_modules() -> None:
    """import 所有 ORM 模块，确保 Base.metadata 完整（建表 / TRUNCATE 都依赖它）。"""
    import server.app.modules.accounts.models  # noqa: F401
    import server.app.modules.ai_generation.models  # noqa: F401
    import server.app.modules.articles.models  # noqa: F401
    import server.app.modules.audit.models  # noqa: F401
    import server.app.modules.image_library.models  # noqa: F401
    import server.app.modules.pipelines.models  # noqa: F401
    import server.app.modules.prompt_templates.models  # noqa: F401
    import server.app.modules.skills.models  # noqa: F401
    import server.app.modules.system.models  # noqa: F401
    import server.app.modules.tasks.models  # noqa: F401


def _make_engine() -> Engine:
    return create_engine(
        get_test_database_url(),
        pool_pre_ping=True,
        connect_args={"init_command": "SET SESSION time_zone='+00:00'"},
    )


def build_test_engine() -> Engine:
    """全新 engine + 全量重置 schema。供需要自管 schema 生命周期的测试用
    （如 test_models / test_fts_and_migrations 的迁移用例）。"""
    engine = _make_engine()
    reset_test_database(engine)
    return engine


def reset_test_database(engine: Engine, *, create_schema: bool = True) -> None:
    """DROP 全部表（+ alembic_version），可选重建 schema + 全文索引。

    任何调用都把共享 schema 缓存标记失效——因为它把库重置成了已知/空状态，
    下个 build_test_app 必须重新确认 schema（重建后再缓存）。"""
    global _schema_ready
    _model_modules()

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

    _schema_ready = False


def _truncate_all(engine: Engine) -> None:
    """清空所有表的数据（保留 schema / 索引），比 DROP+CREATE 快。
    TRUNCATE 会重置 AUTO_INCREMENT，等价于新建表的 id 行为，依赖 id=1 的测试不受影响。"""
    _model_modules()
    with engine.connect() as conn:
        conn.execute(sa.text("SET FOREIGN_KEY_CHECKS=0"))
        try:
            for table in Base.metadata.sorted_tables:
                conn.execute(sa.text(f"TRUNCATE TABLE `{table.name}`"))
        finally:
            conn.execute(sa.text("SET FOREIGN_KEY_CHECKS=1"))
        conn.commit()


def _ensure_clean_db(engine: Engine) -> None:
    """给 build_test_app 用：首次（或 schema 失效后）全量重建并缓存；之后只 TRUNCATE。"""
    global _schema_ready
    if _schema_ready:
        _truncate_all(engine)
    else:
        reset_test_database(engine, create_schema=True)  # 内部会置 _schema_ready=False
        _schema_ready = True


def invalidate_test_schema() -> None:
    """测试若用裸 DDL 改了 schema（如 DROP INDEX），调用本函数让下个 build_test_app 重建。"""
    global _schema_ready
    _schema_ready = False


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

    engine = _make_engine()
    _ensure_clean_db(engine)
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

    return TestApp(
        client=client, data_dir=data_dir, session_factory=TestingSessionLocal, engine=engine
    )


def create_extra_user(
    app: TestApp, username: str, role: str = "operator", password: str = "pw-123456"
) -> tuple[int, TestClient]:
    """在已建好的 test app 上再造一个用户，返回 (user_id, 带其登录 cookie 的新 TestClient)。

    build_test_app 默认只建一个 admin；跨用户隔离 / 越权 404 测试需要第二个（通常是 operator）
    身份。新 client 复用同一个 app（含 get_db override），只是带不同的 access_token cookie。
    """
    from server.app.core.security import create_access_token
    from server.app.modules.system.models import User

    with app.session_factory() as db:
        user = User(username=username, role=role, is_active=True, must_change_password=False)
        user.set_password(password)
        db.add(user)
        db.commit()
        db.refresh(user)
        uid = user.id
        token = create_access_token(user.id, user.role)
    client = TestClient(app.client.app)
    client.cookies["access_token"] = token
    return uid, client
