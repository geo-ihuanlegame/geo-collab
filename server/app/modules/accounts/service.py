"""账号增删改查与本地存储路径换算。

核心是一组在 platform_code/account_key 与 data 目录下相对路径之间互转的纯函数：
storage_state.json（Playwright 登录态）、profile（Chromium 持久化目录）、
profile_key（跨进程 profile 锁的键）都由这里统一推导，确保 worker 与 API 端算法一致。
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from server.app.core.paths import get_data_dir
from server.app.core.time import utcnow
from server.app.modules.accounts.models import Account, AccountMember
from server.app.modules.accounts.schemas import AccountUpdateRequest, ApiAccountCreate
from server.app.modules.system.models import Platform, User
from server.app.modules.tasks.drivers.wechat_client import (
    TOKEN_REFRESH_SKEW_SECONDS,
    WeChatApiError,
    make_default_client,
)
from server.app.modules.tasks.drivers.wechat_client import (
    fetch_access_token as wechat_fetch_access_token,
)
from server.app.modules.tasks.models import PublishRecord, PublishTaskAccount
from server.app.shared.errors import ClientError, ConflictError, ValidationError

_API_PLATFORM_LABELS = {"wechat_mp": "微信公众号"}
_API_PLATFORM_CODES = set(_API_PLATFORM_LABELS)


def normalize_account_key(account_key: str | None) -> str:
    """把账号标识收敛成只含字母数字 _-的安全目录名；为空 / 清洗后为空则随机生成一个。"""
    raw = account_key or uuid.uuid4().hex
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-")
    return value or uuid.uuid4().hex


def state_dir_for_key(platform_code: str, account_key: str, user_id: int | None = None) -> Path:
    if user_id is None:
        return get_data_dir() / "browser_states" / platform_code / account_key
    return get_data_dir() / "browser_states" / "users" / str(user_id) / platform_code / account_key


def state_path_for_key(platform_code: str, account_key: str, user_id: int | None = None) -> Path:
    return state_dir_for_key(platform_code, account_key, user_id=user_id) / "storage_state.json"


def profile_dir_for_key(platform_code: str, account_key: str, user_id: int | None = None) -> Path:
    return state_dir_for_key(platform_code, account_key, user_id=user_id) / "profile"


def state_dir_from_state_path(state_path: str) -> Path:
    return get_data_dir() / Path(state_path).parent


def profile_dir_from_state_path(state_path: str) -> Path:
    return state_dir_from_state_path(state_path) / "profile"


def profile_key_from_state_path(state_path: str) -> str:
    """从 state_path 推出 profile 锁的键（即 storage_state.json 所在目录的相对路径）。

    超过列宽（profile_key 列 255）时退化成 sha256 摘要，保证能落进 DB 主键列。
    """
    key = Path(state_path).parent.as_posix()
    if len(key) <= 240:
        return key
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def clear_profile_locks(profile_dir: Path) -> None:
    """删掉 Chromium 留在持久化 profile 目录里的 Singleton* 残锁。

    上次浏览器进程非正常退出会遗留这些文件，导致下次 launch_persistent_context 拒绝启动。
    """
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock = profile_dir / name
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


def relative_to_data_dir(path: Path) -> str:
    return path.resolve().relative_to(get_data_dir().resolve()).as_posix()


def account_key_from_state_path(state_path: str) -> tuple[str, str]:
    """从 state_path 反解出 (platform_code, account_key)，是 state_path_for_key 的逆运算。

    兼容两种布局：新版带用户隔离 browser_states/users/<uid>/<platform>/<key>/...，
    旧版无用户层 browser_states/<platform>/<key>/...（由 idx+1 是否为 "users" 区分）。
    """
    parts = Path(state_path).parts
    try:
        idx = parts.index("browser_states")
        if parts[idx + 1] == "users":
            return parts[idx + 3], parts[idx + 4]
        return parts[idx + 1], parts[idx + 2]
    except (ValueError, IndexError):
        raise ClientError(f"Invalid state path: {state_path}") from None


def get_or_create_platform(db: Session, code: str, name: str, base_url: str | None) -> Platform:
    platform = db.execute(select(Platform).where(Platform.code == code)).scalar_one_or_none()
    if platform is not None:
        return platform

    platform = Platform(code=code, name=name, base_url=base_url, enabled=True)
    db.add(platform)
    db.flush()
    db.refresh(platform)
    return platform


def launch_options(channel: str, executable_path: str | None) -> dict[str, Any]:
    options: dict[str, Any] = {
        "headless": False,
        "viewport": {"width": 1440, "height": 900},
        "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    }
    if channel and channel.lower() != "chromium":
        options["channel"] = channel
    if executable_path:
        path = Path(executable_path)
        if not path.is_absolute():
            raise ClientError(f"Executable path must be absolute: {executable_path}")
        if not path.is_file():
            raise ClientError(f"Executable not found: {executable_path}")
        options["executable_path"] = executable_path
    return options


def list_accounts(db: Session, q: str | None = None) -> list[Account]:
    """账号列表。``q`` 为可选泛搜索关键词：对 账号名称 / 备注 / 联系方式(手机号) 三字段做
    不区分大小写的包含匹配（任一命中即返回）。空白 q 视为不过滤。"""
    stmt = (
        select(Account)
        .where(Account.is_deleted == False)  # noqa: E712
        .options(selectinload(Account.platform))
        .order_by(Account.updated_at.desc())
    )
    keyword = (q or "").strip()
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            or_(
                Account.display_name.ilike(like),
                Account.note.ilike(like),
                Account.contact.ilike(like),
            )
        )
    return list(db.execute(stmt).scalars().all())


def get_account(db: Session, account_id: int) -> Account | None:
    stmt = (
        select(Account)
        .where(Account.id == account_id, Account.is_deleted == False)  # noqa: E712
        .options(selectinload(Account.platform))
    )
    return db.execute(stmt).scalar_one_or_none()


def is_api_platform_code(code: str) -> bool:
    from server.app.modules.tasks.drivers import is_api_driver

    return code in _API_PLATFORM_CODES or is_api_driver(code)


def api_platform_options() -> list[dict[str, str]]:
    return [
        {"code": code, "name": name}
        for code, name in sorted(_API_PLATFORM_LABELS.items(), key=lambda item: item[0])
    ]


def create_api_account(db: Session, user_id: int, payload: ApiAccountCreate) -> Account:
    """创建 API 型平台账号：凭据直存，platform_user_id 取 AppID 防重复登记。"""
    if not is_api_platform_code(payload.platform_code):
        raise ValidationError(f"平台 {payload.platform_code} 为浏览器登录接入，请走扫码授权流程")

    platform = db.execute(
        select(Platform).where(Platform.code == payload.platform_code)
    ).scalar_one_or_none()
    if platform is None:
        raise ValidationError(f"平台不存在: {payload.platform_code}")

    app_id = payload.api_credentials.app_id
    duplicate = db.execute(
        select(Account.id).where(
            Account.platform_id == platform.id,
            Account.platform_user_id == app_id,
            Account.is_deleted == False,  # noqa: E712
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ConflictError(f"该 AppID 已被登记（全平台唯一）: {app_id}")

    account = Account(
        user_id=user_id,
        platform=platform,
        display_name=payload.display_name.strip(),
        platform_user_id=app_id,
        status="unknown",
        state_path=None,
        api_credentials={
            "app_id": app_id,
            "app_secret": payload.api_credentials.app_secret,
        },
        contact=payload.contact,
        note=payload.note,
        avatar_asset_id=payload.avatar_asset_id,
        distribution_enabled=payload.distribution_enabled,
    )
    db.add(account)
    try:
        db.flush()
    except IntegrityError as exc:  # 并发抢注同一 app_id：DB 全局唯一约束兜底
        db.rollback()
        raise ConflictError(f"该 AppID 已被登记（全平台唯一）: {app_id}") from exc
    return get_account(db, account.id) or account


def _ensure_app_id_available(db: Session, account: Account, app_id: str) -> None:
    duplicate = db.execute(
        select(Account.id).where(
            Account.platform_id == account.platform_id,
            Account.platform_user_id == app_id,
            Account.is_deleted == False,  # noqa: E712
            Account.id != account.id,
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ConflictError(f"该 AppID 已被登记（全平台唯一）: {app_id}")


def update_account_fields(db: Session, account: Account, payload: AccountUpdateRequest) -> Account:
    """通用 PATCH：仅更新显式传入的非 None 字段；api_credentials 整体替换。"""
    data = payload.model_dump(exclude_unset=True)
    if data.get("display_name") is not None:
        account.display_name = data["display_name"].strip()
    if data.get("contact") is not None:
        account.contact = data["contact"]
    if data.get("note") is not None:
        account.note = data["note"]
    if data.get("avatar_asset_id") is not None:
        account.avatar_asset_id = data["avatar_asset_id"]
    if data.get("distribution_enabled") is not None:
        account.distribution_enabled = data["distribution_enabled"]
    if data.get("api_credentials") is not None:
        if not is_api_platform_code(account.platform.code):
            raise ValidationError("浏览器登录平台不支持 API 凭据")
        assert payload.api_credentials is not None  # type guard for mypy
        creds = payload.api_credentials
        _ensure_app_id_available(db, account, creds.app_id)
        account.api_credentials = {"app_id": creds.app_id, "app_secret": creds.app_secret}
        account.platform_user_id = creds.app_id
        account.api_token_cache = None
        account.status = "unknown"
        account.last_checked_at = None
    account.updated_at = utcnow()
    db.flush()
    return get_account(db, account.id) or account


def verify_api_credentials(db: Session, account: Account) -> Account:
    """强制刷新 access_token 验证凭据；失败时置 expired 并透传微信错误。"""
    if not is_api_platform_code(account.platform.code):
        raise ValidationError("该平台为浏览器登录接入，无凭据可验证")
    creds = account.api_credentials or {}
    if not creds.get("app_id") or not creds.get("app_secret"):
        raise ValidationError("账号未配置 AppID/AppSecret")

    client = make_default_client()
    try:
        token, expires_in = wechat_fetch_access_token(
            creds["app_id"], creds["app_secret"], client=client
        )
    except WeChatApiError as exc:
        account.status = "expired"
        account.api_token_cache = None
        account.last_checked_at = utcnow()
        account.updated_at = account.last_checked_at
        db.flush()
        raise ValidationError(str(exc)) from exc
    finally:
        client.close()

    account.api_token_cache = {
        "access_token": token,
        "expires_at": int(time.time()) + int(expires_in),
    }
    account.status = "valid"
    account.last_checked_at = utcnow()
    account.updated_at = account.last_checked_at
    db.flush()
    return get_account(db, account.id) or account


def get_cached_wechat_token(account: Account) -> str | None:
    if account.status != "valid":
        return None
    cache = account.api_token_cache or {}
    token = cache.get("access_token")
    expires_at = int(cache.get("expires_at") or 0)
    if not token or expires_at <= int(time.time()) + TOKEN_REFRESH_SKEW_SECONDS:
        return None
    return str(token)


def rename_account(db: Session, account: Account, display_name: str) -> Account:
    account.display_name = display_name.strip()
    account.updated_at = utcnow()
    db.flush()
    return get_account(db, account.id) or account


def delete_account(db: Session, account: Account) -> None:
    """软删账号并释放身份槽位；仍有未完成发布记录时抛 ClientError 拒绝删除。

    释放槽位＝置空 platform_user_id（全局唯一约束据此放行同一 app_id 重新登记）、
    清 api_token_cache、抹除 api_credentials.app_secret（保留 app_id 供审计）。
    发布历史（PublishRecord.account_id）不动。
    """
    account_id = account.id

    active = (
        db.execute(
            select(PublishRecord.id).where(
                PublishRecord.account_id == account_id,
                PublishRecord.status.in_(
                    ["pending", "running", "waiting_manual_publish", "waiting_user_input"]
                ),
            )
        )
        .scalars()
        .all()
    )
    if active:
        raise ClientError("存在未完成发布记录，无法删除账号")

    account.is_deleted = True
    account.deleted_at = utcnow()
    account.platform_user_id = None
    account.api_token_cache = None
    if account.api_credentials:
        creds = dict(account.api_credentials)
        creds.pop("app_secret", None)
        account.api_credentials = creds or None
    account.updated_at = utcnow()
    db.flush()


def _get_driver(platform_code: str):
    from server.app.modules.tasks.drivers import get_driver

    return get_driver(platform_code)


# ── 共享账号鉴权判定（全模块复用，见设计稿 §6）────────────────────────────────


def user_can_use_account(db: Session, account: Account, user: User) -> bool:
    """该用户能否「使用」此账号：admin 或 owner 或已被授予的成员。"""
    if user.role == "admin":
        return True
    if account.user_id == user.id:
        return True
    member = db.execute(
        select(AccountMember.user_id).where(
            AccountMember.account_id == account.id,
            AccountMember.user_id == user.id,
        )
    ).scalar_one_or_none()
    return member is not None


def user_can_manage_account(account: Account, user: User) -> bool:
    """该用户能否「管理」此账号：admin 或 owner（成员仅可见 + 使用，不可管理）。"""
    return user.role == "admin" or account.user_id == user.id


# ── 共享去重合并函数（§4 登录去重 / §5 回填 / admin 批量回填唯一落点，见设计稿 §4a）──────


def reconcile_duplicate_into_canonical(
    db: Session, dup: Account, canonical: Account, *, granted_via: str
) -> None:
    """把 dup 行并入 canonical：加成员 + 条件合并 dup 行 + 审计。全程幂等、可重试。

    1. 加成员：dup.user_id 既非 canonical.user_id（owner）又不在 account_members → 插一行；
       已是 owner / 已是成员 → 跳过。
    2. 条件合并 dup 行：
       - dup 无未软删 PublishRecord 且无任务绑定 → 软删 dup（释放身份槽位）。
       - dup 有记录 / 任务 → 不软删，置 dup.merged_into = canonical.id。
       - dup 已 is_deleted 或 merged_into 已指向 canonical.id → 幂等跳过合并。
    3. 审计：写一条 account.dedup_merge。

    本函数只做上述合并写，**不写 platform_user_id、不 claim X、不 commit**——commit 由
    调用方统一收口，并发 IntegrityError 兜底也在调用方（见设计稿 §4 / §5 / §4a）。
    """
    from server.app.modules.audit.service import add_audit_entry

    # 1) 加成员（幂等）：非 owner 且未在成员表才插
    if dup.user_id != canonical.user_id:
        existing = db.execute(
            select(AccountMember.user_id).where(
                AccountMember.account_id == canonical.id,
                AccountMember.user_id == dup.user_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                AccountMember(
                    account_id=canonical.id,
                    user_id=dup.user_id,
                    granted_via=granted_via,
                )
            )

    # 2) 条件合并 dup 行（幂等跳过已终态情形）
    already_merged = dup.is_deleted or dup.merged_into == canonical.id
    if not already_merged:
        has_record = (
            db.execute(
                select(PublishRecord.id)
                .where(
                    PublishRecord.account_id == dup.id,
                    PublishRecord.is_deleted == False,  # noqa: E712
                )
                .limit(1)
            ).scalar_one_or_none()
            is not None
        )
        has_task = (
            db.execute(
                select(PublishTaskAccount.id)
                .where(PublishTaskAccount.account_id == dup.id)
                .limit(1)
            ).scalar_one_or_none()
            is not None
        )
        if has_record or has_task:
            # 有历史 / 任务：保留行让未终态发布记录发完，仅标 merged_into
            dup.merged_into = canonical.id
        else:
            # 干净行：软删并释放身份槽位（platform_user_id 置 NULL）
            dup.is_deleted = True
            dup.deleted_at = utcnow()
            dup.platform_user_id = None
        dup.updated_at = utcnow()

    # 3) 审计（add_audit_entry 内部自吞异常、自 commit，但这里 best-effort 即可）
    add_audit_entry(
        db,
        user=None,
        action="account.dedup_merge",
        target_type="account",
        target_id=canonical.id,
        payload={
            "dup_id": dup.id,
            "canonical_id": canonical.id,
            "granted_via": granted_via,
        },
    )
