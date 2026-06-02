"""MySQL engine and Session factory."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from server.app.core.paths import ensure_data_dirs, get_database_url

ensure_data_dirs()

_db_url = get_database_url()
engine = create_engine(
    _db_url,
    pool_size=5,
    max_overflow=10,
    pool_recycle=3600,
    pool_pre_ping=True,
    connect_args={"init_command": "SET SESSION time_zone='+00:00'"},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# FastAPI 依赖注入：每个请求获取一个新 Session
# 自动 commit（成功）或 rollback（异常）
def get_db():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
