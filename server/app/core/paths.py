"""数据目录与数据库 URL 解析。

- get_data_dir / ensure_data_dirs：解析 GEO_DATA_DIR 并保证四个子目录存在。
- get_database_url：仅支持 MySQL，优先 GEO_DATABASE_URL，否则拼 GEO_DB_*。
"""

from pathlib import Path
from urllib.parse import quote_plus, urlparse

from server.app.core.config import get_settings

# 数据目录下的子目录列表
DATA_SUBDIRS = ("assets", "browser_states", "logs", "exports")


# 解析已配置的数据目录
def get_data_dir() -> Path:
    data_dir = get_settings().data_dir
    if data_dir is None:
        raise RuntimeError("GEO_DATA_DIR not set")
    return data_dir


# 获取 MySQL SQLAlchemy 连接 URL
def get_database_url() -> str:
    settings = get_settings()
    if settings.database_url:
        parsed = urlparse(settings.database_url)
        # 仅支持 MySQL：拒绝非 mysql+pymysql 驱动（无 SQLite 兼容）
        if parsed.scheme != "mysql+pymysql":
            raise RuntimeError("GEO_DATABASE_URL must use mysql+pymysql://")
        return settings.database_url
    if settings.db_host and settings.db_user and settings.db_name:
        # quote_plus 转义密码中的特殊字符，无需手动做 URL 编码
        password = quote_plus(settings.db_pass or "")
        return f"mysql+pymysql://{settings.db_user}:{password}@{settings.db_host}:{settings.db_port}/{settings.db_name}"
    raise RuntimeError(
        "MySQL database configuration is required: set GEO_DATABASE_URL or GEO_DB_HOST/GEO_DB_USER/GEO_DB_NAME"
    )


# 确保数据目录及所有子目录存在
def ensure_data_dirs() -> Path:
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    for subdir in DATA_SUBDIRS:
        (data_dir / subdir).mkdir(parents=True, exist_ok=True)
    return data_dir
