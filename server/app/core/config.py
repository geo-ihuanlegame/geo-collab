"""
应用配置，环境变量前缀 GEO_。

使用方式：
  get_settings() → Settings（@lru_cache 单例）
  测试中环境变更后需调用 get_settings.cache_clear()

本地开发关键配置：
  GEO_DATA_DIR                    数据目录（必填）
  GEO_PUBLISH_MAX_CONCURRENT_RECORDS  并发发布记录数（上限 5）
  GEO_PUBLISH_PRE_DELAY_ENABLED       发布前随机延迟开关（默认 true，范围 GEO_PUBLISH_PRE_DELAY_MIN/MAX_SECONDS，默认 10/120）

云端远程浏览器配置：
  GEO_PUBLISH_XVFB_PATH               Xvfb 可执行路径
  GEO_PUBLISH_X11VNC_PATH             x11vnc 可执行路径
  GEO_PUBLISH_WEBSOCKIFY_PATH         websockify 可执行路径
  GEO_PUBLISH_NOVNC_WEB_DIR           noVNC 静态文件目录
  GEO_PUBLISH_REMOTE_BROWSER_HOST     对外暴露的主机地址（默认 127.0.0.1）
"""

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from server.app.shared.resilience import RetryPolicy


class AiEngineConfig(BaseModel):
    """单个写作引擎：label + litellm 模型串 + 自带密钥/网关。

    通过 GEO_AI_ENGINES 传 JSON 数组覆盖。下拉选中存的是 model 串，
    后端按 model 串回查本配置拿 api_key / base_url（见 resolve_engine）。
    """

    label: str
    model: str = ""  # "" = 用 settings.ai_model 默认写作模型
    api_key: str = ""  # "" = 回落到 settings.ai_api_key
    base_url: str | None = None  # OpenAI 兼容网关/代理；None = litellm 默认


class Settings(BaseSettings):
    app_name: str = "Geo Collab"
    app_version: str = "0.1.0"
    data_dir: Path | None = None
    # 日志（详见 core/logging.py）：级别 + 是否落滚动文件到 GEO_DATA_DIR/logs/app.log。
    log_level: str = "INFO"  # GEO_LOG_LEVEL：DEBUG/INFO/WARNING/ERROR
    log_to_file: bool = True  # GEO_LOG_TO_FILE：关掉则只打 stdout
    log_file_backup_days: int = 14  # GEO_LOG_FILE_BACKUP_DAYS：按天滚动保留份数
    database_url: str | None = None
    # 独立数据库凭据（当 database_url 未设时自动拼接 MySQL 连接 URL，密码无需手动做 URL 编码）
    db_host: str | None = None
    db_port: int = 3306
    db_user: str | None = None
    db_pass: str | None = None
    db_name: str | None = None
    jwt_secret: str = ""
    publish_max_concurrent_records: int = 5
    publish_record_timeout_seconds: int = 300
    # 断网/弱网发布重试（见 docs/superpowers/specs/2026-06-23-publish-network-retry-design.md）
    publish_retry_enabled: bool = True  # GEO_PUBLISH_RETRY_ENABLED
    publish_retry_max_attempts: int = 3  # GEO_PUBLISH_RETRY_MAX_ATTEMPTS（含首次）
    publish_retry_base_delay_seconds: float = 1.0  # GEO_PUBLISH_RETRY_BASE_DELAY_SECONDS
    publish_retry_max_delay_seconds: float = 15.0  # GEO_PUBLISH_RETRY_MAX_DELAY_SECONDS
    publish_retry_max_elapsed_seconds: float = 60.0  # GEO_PUBLISH_RETRY_MAX_ELAPSED_SECONDS
    # 发布前随机延迟（错峰防封）。enabled 默认开启；每条发布在调用驱动发文前
    # sleep random.uniform(min, max) 秒。stop_before_publish 的人工确认流程不延迟。
    publish_pre_delay_enabled: bool = True  # GEO_PUBLISH_PRE_DELAY_ENABLED
    publish_pre_delay_min_seconds: float = 10.0  # GEO_PUBLISH_PRE_DELAY_MIN_SECONDS
    publish_pre_delay_max_seconds: float = 120.0  # GEO_PUBLISH_PRE_DELAY_MAX_SECONDS
    login_max_concurrent_browsers: int = 8  # GEO_LOGIN_MAX_CONCURRENT_BROWSERS
    publish_browser_channel: str = "chromium"
    publish_browser_executable_path: str | None = None
    # 发布路径 Chromium 是否 headless（GEO_PUBLISH_BROWSER_HEADLESS）。默认 True=headless（发布路径
    # 不起 VNC 链，大幅降低单次发布内存/CPU）；设 false 回退 headed+VNC+实时接管（noVNC 人工干预
    # 场景）。仅作用于发布；**登录路径恒为 headed**（人工扫码必须可见）。
    publish_browser_headless: bool = True  # GEO_PUBLISH_BROWSER_HEADLESS
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
    question_pool_sync_interval_seconds: int = (
        21600  # GEO_QUESTION_POOL_SYNC_INTERVAL_SECONDS（6 小时）
    )
    pipeline_scheduler_enabled: bool = False  # GEO_PIPELINE_SCHEDULER_ENABLED
    pipeline_scheduler_interval_seconds: int = 60  # GEO_PIPELINE_SCHEDULER_INTERVAL_SECONDS
    scheduler_tz: str = "Asia/Shanghai"  # GEO_SCHEDULER_TZ
    # TapTap cookie 体检（应用内后台线程，纯 HTTP 探测 account-profile/v1/me）。默认关闭。
    taptap_cookie_check_enabled: bool = False  # GEO_TAPTAP_COOKIE_CHECK_ENABLED
    taptap_cookie_check_interval_seconds: int = (
        43200  # GEO_TAPTAP_COOKIE_CHECK_INTERVAL_SECONDS（12 小时）
    )
    # 账号登录态夜间保活（worker 后台线程，复用检测按键无头刷新 storage_state）。默认关闭。
    # 见 docs/superpowers/specs/2026-06-25-account-login-keepalive-design.md
    account_keepalive_enabled: bool = False  # GEO_ACCOUNT_KEEPALIVE_ENABLED
    account_keepalive_window_start: str = (
        "23:00"  # GEO_ACCOUNT_KEEPALIVE_WINDOW_START（HH:MM，scheduler_tz）
    )
    account_keepalive_window_end: str = "03:00"  # GEO_ACCOUNT_KEEPALIVE_WINDOW_END（跨午夜）
    account_keepalive_min_gap_seconds: int = 30  # GEO_ACCOUNT_KEEPALIVE_MIN_GAP_SECONDS
    account_keepalive_max_gap_seconds: int = 600  # GEO_ACCOUNT_KEEPALIVE_MAX_GAP_SECONDS（10min）
    account_keepalive_poll_seconds: int = (
        120  # GEO_ACCOUNT_KEEPALIVE_POLL_SECONDS（窗口外/无待刷轮询步长）
    )
    account_keepalive_check_timeout_seconds: int = (
        120  # GEO_ACCOUNT_KEEPALIVE_CHECK_TIMEOUT_SECONDS（单账号看门狗）
    )
    run_startup_recovery: bool = True  # GEO_RUN_STARTUP_RECOVERY；多实例只在单一实例开启
    # 资源指标周期采样（Task 3，封堵 #10）。后台守护线程每 N 秒采一份池/run 快照打点到日志，
    # checked_out/max 超阈值升 WARNING。默认开启、可关闭；采样纯内存读 + 轻量 COUNT，不改并发。
    resource_metrics_sampling_enabled: bool = True  # GEO_RESOURCE_METRICS_SAMPLING_ENABLED
    resource_metrics_sample_interval_seconds: int = (
        60  # GEO_RESOURCE_METRICS_SAMPLE_INTERVAL_SECONDS
    )
    resource_metrics_warn_ratio: float = (
        0.8  # GEO_RESOURCE_METRICS_WARN_RATIO；checked_out/max 超此比例升 WARNING
    )
    # 连接预算安全余量（Task 5，封堵 #4）：anyio 线程池 + 发布并发封顶 + 本余量 ≤ 连接池容量。
    # 余量吸收 anyio 之外的零散 checkout：pipeline/scheme 后台 run 的瞬时借连接（各自闸封顶 3+2）、
    # SSE 短借、启动期 recovery、定时任务。仅此一个旋钮——越界时杠杆是扩池
    # （GEO_DB_POOL_SIZE/GEO_DB_MAX_OVERFLOW）或降发布并发，绝不缩 anyio。
    connection_budget_safety_margin: int = 10  # GEO_CONNECTION_BUDGET_SAFETY_MARGIN
    ai_generate_max_count: int = 20  # GEO_AI_GENERATE_MAX_COUNT
    pipeline_max_concurrent_runs: int = 3  # GEO_PIPELINE_MAX_CONCURRENT_RUNS
    scheme_max_concurrent_runs: int = 2  # GEO_SCHEME_MAX_CONCURRENT_RUNS（方案运行全局并发闸）
    # 等并发槽超时（秒）：超时即把 run 置 failed、不无限阻塞（#9）。默认给够单 run ~25min 占槽下的排队余量。
    pipeline_run_acquire_timeout_seconds: int = 1800  # GEO_PIPELINE_RUN_ACQUIRE_TIMEOUT_SECONDS
    scheme_run_acquire_timeout_seconds: int = 1800  # GEO_SCHEME_RUN_ACQUIRE_TIMEOUT_SECONDS
    # [临时] 方案生文封面兜底存储桶（GEO_TEMP_COVER_BUCKET）。空字符串=禁用整段临时封面逻辑。
    temp_cover_bucket: str = "cantingyangchengji"
    # AI 生文（LangGraph 写作智能体）—— 保持 Claude
    # NOTE: LiteLLM 1.40+ 要求 model 串显式带 provider 前缀（如 anthropic/、openai/、deepseek/），
    # 不再自动猜。无前缀串会抛 BadRequestError: "LLM Provider NOT provided"。
    ai_model: str = "anthropic/claude-3-5-sonnet-20241022"  # GEO_AI_MODEL
    ai_api_key: str = ""  # GEO_AI_API_KEY
    # 方案级可选 AI 引擎列表（为后续接入更多写作模型留接口）。
    # 每项 = AiEngineConfig（label 展示名 / model litellm 串 / api_key / base_url）。
    # model 空 = 用 ai_model 默认；api_key 空 = 回落 ai_api_key；base_url 留空 = litellm 默认。
    # 通过 GEO_AI_ENGINES 传 JSON 覆盖，例如：
    #   [{"label":"DeepSeek","model":"deepseek/deepseek-chat","api_key":"sk-ds"},
    #    {"label":"网关","model":"openai/gpt-4o","api_key":"sk-x","base_url":"https://oneapi/v1"}]
    ai_engines: list[AiEngineConfig] = [AiEngineConfig(label="默认写作模型")]  # GEO_AI_ENGINES

    # AI 格式调整（标题识别 / 未来配图配链接）—— 独立模型，降低成本
    ai_format_model: str = "deepseek/deepseek-v4-flash"  # GEO_AI_FORMAT_MODEL
    ai_format_api_key: str = ""  # GEO_AI_FORMAT_API_KEY
    ai_format_timeout_seconds: int = 120  # GEO_AI_FORMAT_TIMEOUT_SECONDS

    # MinIO 图片库存储
    minio_endpoint: str = "localhost:9000"  # GEO_MINIO_ENDPOINT
    minio_access_key: str = ""  # GEO_MINIO_ACCESS_KEY
    minio_secret_key: str = ""  # GEO_MINIO_SECRET_KEY
    minio_secure: bool = False  # GEO_MINIO_SECURE

    # AI配图「联网兜底」：陪衬游戏库里无图时，用百度千帆 AI 搜索拉真实横版图补充
    # Key 启动时不校验，缺失时走兜底的请求里才报错（与 AI Key 一致）
    baidu_api_key: str = ""  # GEO_BAIDU_API_KEY（千帆 Bearer API Key）
    baidu_ai_search_url: str = (
        "https://qianfan.baidubce.com/v2/ai_search/web_search"  # GEO_BAIDU_AI_SEARCH_URL
    )
    baidu_ai_search_timeout_seconds: int = 30  # GEO_BAIDU_AI_SEARCH_TIMEOUT_SECONDS
    # 限流韧性：配图并发 + 同名重复会把千帆 QPS 打爆（实测整批 429）。下面三个旋钮收口在 baidu.py。
    baidu_min_interval_seconds: float = (
        0.33  # GEO_BAIDU_MIN_INTERVAL_SECONDS 全局限速(串行间隔)，~3 QPS（贴近千帆 ~3-4 QPS 上限；多 worker 下需改跨进程限速）
    )
    baidu_max_retries: int = (
        3  # GEO_BAIDU_MAX_RETRIES 遇 429 的重试次数（认 Retry-After，否则指数退避）
    )
    baidu_neg_cache_seconds: int = (
        120  # GEO_BAIDU_NEG_CACHE_SECONDS 同名搜图失败的负缓存 TTL，省得本批反复打
    )

    # MCP server（Claude Code 通过 stdio spawn 调用 GEO 能力）
    # 注意：MCP server 子进程的 GEO_API_BASE_URL 由 server/mcp/config.py 直接读 os.environ，
    # 不进 Settings——避免与服务端进程的 mcp_token 校验路径耦合。
    mcp_token: str = ""  # GEO_MCP_TOKEN（独立 service token，与 user JWT 隔离；空=禁用 MCP）

    # 敏感凭据静态加密（app_secret / token / storage_state）。
    # 空 = NullCipher 透传（本地/测试零配置）；prod 设密钥才真加密。
    # 密钥 = Fernet urlsafe-base64 32 字节，用 `python -m server.scripts.gen_secret_key` 生成。
    secret_key: str = ""  # GEO_SECRET_KEY，单密钥
    secret_keys: str = ""  # GEO_SECRET_KEYS，逗号分隔多密钥（轮换；非空时优先于 secret_key）

    model_config = SettingsConfigDict(env_prefix="GEO_", env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_publish_retry_policy() -> "RetryPolicy":
    """由 Settings 构建发布重试策略。注意：max_elapsed 必须 < 单记录执行预算
    （publish_record_timeout_seconds），否则 watchdog 会先于重试杀掉记录。"""
    from server.app.shared.resilience import RetryPolicy

    s = get_settings()
    return RetryPolicy(
        enabled=s.publish_retry_enabled,
        max_attempts=s.publish_retry_max_attempts,
        base_delay=s.publish_retry_base_delay_seconds,
        max_delay=s.publish_retry_max_delay_seconds,
        max_elapsed=s.publish_retry_max_elapsed_seconds,
    )


def resolve_engine(selected: str | None) -> tuple[str, str, str | None]:
    """下拉选中的 model 串 → 实际调用参数 (model, api_key, base_url)。

    - 空串 / None：系统默认引擎（ai_model + ai_api_key）。
    - 命中 ai_engines 某项：用该项 model（空则默认）、api_key（空则回落默认 key）、base_url。
    - 列表里没有（手填 / 历史值）：原样用该 model + 默认 key。
    """
    settings = get_settings()
    sel = (selected or "").strip()
    if not sel:
        return settings.ai_model, settings.ai_api_key, None
    for e in settings.ai_engines:
        if e.model == sel:
            return e.model or settings.ai_model, e.api_key or settings.ai_api_key, e.base_url
    return sel, settings.ai_api_key, None


# 文件上传大小限制
MAX_ASSET_BYTES: int = 20 * 1024 * 1024  # 20 MB
MAX_ZIP_BYTES: int = 50 * 1024 * 1024  # 50 MB

# 允许上传图片的文件魔数
ALLOWED_MAGIC: list[bytes] = [
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"\xff\xd8",  # JPEG
    b"RIFF",  # WebP（还需检查 bytes 8:12 == b"WEBP"）
    b"GIF87a",  # GIF
    b"GIF89a",  # GIF
]
