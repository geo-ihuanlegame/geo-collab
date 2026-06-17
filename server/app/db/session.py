"""MySQL 引擎与 Session 工厂。"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from server.app.core.paths import ensure_data_dirs, get_database_url

# 副作用：导入本模块即创建数据目录（多处模块导入时就依赖目录已存在）
ensure_data_dirs()


def _env_int(name: str, default: int) -> int:
    """读取整数环境变量；缺失 / 空 / 非法时回落默认值。

    连接池参数在 import 期（建 engine 时）就要确定，故直接读 os.environ、不走 Settings 缓存，
    与 hot_lists 直读 GEO_HOTLIST_API_URL 的处理一致。
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


_db_url = get_database_url()
engine = create_engine(
    _db_url,
    # 池容量要覆盖「SSE 长连接 + 后台并发生文（pipeline/scheme run 各 ×4 worker）+ 普通请求」峰值。
    # 旧值 5+10=15 在多人演示下被占满 → 全站请求排队 30s 超时（连接池耗尽事故，见 git 历史）。
    # 三个参数都可用环境变量覆盖：改 .env 后重启/重建容器即生效，无需改代码。
    pool_size=_env_int("GEO_DB_POOL_SIZE", 20),
    max_overflow=_env_int("GEO_DB_MAX_OVERFLOW", 40),
    pool_timeout=_env_int("GEO_DB_POOL_TIMEOUT", 10),  # 池满最多等 N 秒，快速失败而非默认 30s 假死
    pool_recycle=3600,  # 回收空闲连接，规避 MySQL wait_timeout 断连
    pool_pre_ping=True,  # 取连接前先 ping，剔除已失效连接
    # 会话时区固定 +00:00：全库时间戳是无时区 UTC（见 core/time.utcnow），数据库不得偏移
    connect_args={"init_command": "SET SESSION time_zone='+00:00'"},
)

# Task G：运行期长持连接护栏（checkout 超阈值才归还即告警，防 #1/#110 同源反模式悄悄复发）。
# 开关 / 阈值走环境变量（GEO_CONNECTION_WATCHDOG_*），与上面池参数同一处理方式；默认开、可关。
from server.app.shared.connection_watchdog import register_connection_watchdog  # noqa: E402

register_connection_watchdog(engine)

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
