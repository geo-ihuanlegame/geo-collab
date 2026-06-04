"""
应用配置，环境变量前缀 GEO_。

使用方式：
  get_settings() → Settings（@lru_cache 单例）
  测试中环境变更后需调用 get_settings.cache_clear()

本地开发关键配置：
  GEO_DATA_DIR                    数据目录（必填）
  GEO_PUBLISH_MAX_CONCURRENT_RECORDS  并发发布记录数（上限 5）

云端远程浏览器配置：
  GEO_PUBLISH_XVFB_PATH               Xvfb 可执行路径
  GEO_PUBLISH_X11VNC_PATH             x11vnc 可执行路径
  GEO_PUBLISH_WEBSOCKIFY_PATH         websockify 可执行路径
  GEO_PUBLISH_NOVNC_WEB_DIR           noVNC 静态文件目录
  GEO_PUBLISH_REMOTE_BROWSER_HOST     对外暴露的 host（默认 127.0.0.1）
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Geo Collab"
    app_version: str = "0.1.0"
    data_dir: Path | None = None
    database_url: str | None = None
    # 独立 DB 凭据（当 database_url 未设时自动拼接 MySQL URL，密码无需手动 URL-encode）
    db_host: str | None = None
    db_port: int = 3306
    db_user: str | None = None
    db_pass: str | None = None
    db_name: str | None = None
    jwt_secret: str = ""
    publish_max_concurrent_records: int = 5
    publish_record_timeout_seconds: int = 300
    publish_browser_channel: str = "chromium"
    publish_browser_executable_path: str | None = None
    publish_xvfb_path: str = "Xvfb"
    publish_x11vnc_path: str = "x11vnc"
    publish_websockify_path: str = "websockify"
    publish_novnc_web_dir: str | None = None
    publish_remote_browser_host: str = "127.0.0.1"
    publish_remote_browser_display_base: int = 99
    publish_remote_browser_vnc_base_port: int = 5900
    publish_remote_browser_novnc_base_port: int = 6080
    publish_remote_browser_start_timeout_seconds: float = 15.0
    publish_remote_browser_idle_timeout_seconds: int = 300  # 5 分钟无操作自动清理
    secure_cookie: bool = False  # 生产 HTTPS 时设为 True（GEO_SECURE_COOKIE=true）
    feishu_webhook_url: str | None = None  # GEO_FEISHU_WEBHOOK_URL，不设则静默跳过
    # 飞书自建应用凭据（问题库从多维表同步、以及未来发布采集写回 都用它换 tenant_access_token）
    feishu_app_id: str | None = None  # GEO_FEISHU_APP_ID
    feishu_app_secret: str | None = None  # GEO_FEISHU_APP_SECRET
    # 问题池定时镜像同步（应用内后台线程）。默认关闭，避免本地 / 测试打真实飞书。
    question_pool_auto_sync_enabled: bool = False  # GEO_QUESTION_POOL_AUTO_SYNC_ENABLED
    question_pool_sync_interval_seconds: int = 21600  # GEO_QUESTION_POOL_SYNC_INTERVAL_SECONDS (6h)
    # AI 生文（LangGraph 写作 Agent）—— 保持 Claude
    ai_model: str = "claude-3-5-sonnet-20241022"  # GEO_AI_MODEL
    ai_api_key: str = ""  # GEO_AI_API_KEY
    # 方案级可选 AI 引擎列表（为后续接入更多写作模型留接口）。
    # 每项 {"label": 展示名, "model": litellm model 字符串}；model 为空 = 用 ai_model 默认。
    # 通过 GEO_AI_ENGINES 传 JSON 覆盖，例如：
    #   [{"label":"默认写作模型","model":""},{"label":"DeepSeek","model":"deepseek/deepseek-chat"}]
    ai_engines: list[dict[str, str]] = [{"label": "默认写作模型", "model": ""}]  # GEO_AI_ENGINES

    # AI 格式调整（标题识别 / 未来配图配链接）—— 独立模型，降低成本
    ai_format_model: str = "deepseek/deepseek-v4-flash"  # GEO_AI_FORMAT_MODEL
    ai_format_api_key: str = ""  # GEO_AI_FORMAT_API_KEY
    ai_format_timeout_seconds: int = 120  # GEO_AI_FORMAT_TIMEOUT_SECONDS

    # MinIO 图片库存储
    minio_endpoint: str = "localhost:9000"  # GEO_MINIO_ENDPOINT
    minio_access_key: str = ""  # GEO_MINIO_ACCESS_KEY
    minio_secret_key: str = ""  # GEO_MINIO_SECRET_KEY
    minio_secure: bool = False  # GEO_MINIO_SECURE

    model_config = SettingsConfigDict(env_prefix="GEO_", env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


# File upload limits
MAX_ASSET_BYTES: int = 20 * 1024 * 1024  # 20 MB
MAX_ZIP_BYTES: int = 50 * 1024 * 1024  # 50 MB

# Allowed magic bytes for image uploads
ALLOWED_MAGIC: list[bytes] = [
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"\xff\xd8",  # JPEG
    b"RIFF",  # WebP (also check bytes 8:12 == b"WEBP")
    b"GIF87a",  # GIF
    b"GIF89a",  # GIF
]
