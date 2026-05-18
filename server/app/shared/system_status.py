from server.app.core.paths import DATA_SUBDIRS, ensure_data_dirs
from server.app.schemas.system import SystemStatus


# 获取系统基础状态（目录、版本等），不依赖数据库
def get_system_status() -> SystemStatus:
    data_dir = ensure_data_dirs()
    directories_ready = data_dir.exists() and all((data_dir / name).exists() for name in DATA_SUBDIRS)

    return SystemStatus(
        service="ok",
        directories_ready=directories_ready,
    )

