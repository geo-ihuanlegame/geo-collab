"""MySQL 引擎与 Session 工厂。"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from server.app.core.paths import ensure_data_dirs, get_database_url

# 副作用：导入本模块即创建数据目录（多处模块导入时就依赖目录已存在）
ensure_data_dirs()

_db_url = get_database_url()
engine = create_engine(
    _db_url,
    pool_size=5,
    max_overflow=10,
    pool_recycle=3600,  # 回收空闲连接，规避 MySQL wait_timeout 断连
    pool_pre_ping=True,  # 取连接前先 ping，剔除已失效连接
    # 会话时区固定 +00:00：全库时间戳是无时区 UTC（见 core/time.utcnow），数据库不得偏移
    connect_args={"init_command": "SET SESSION time_zone='+00:00'"},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# FastAPI 依赖注入：每个请求获取一个新 Session
# 成功时自动提交，异常时自动回滚
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
