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

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from server.app.core.paths import get_data_dir
from server.app.core.time import utcnow
from server.app.modules.accounts.models import Account, AccountMember
from server.app.modules.accounts.schemas import (
    AccountUpdateRequest,
    ApiAccountCreate,
    TaptapForumIn,
)
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


def launch_options(
    channel: str, executable_path: str | None, *, headless: bool = False
) -> dict[str, Any]:
    """构建 Playwright launch_persistent_context 选项。

    headless 仅由发布路径显式传入（GEO_PUBLISH_BROWSER_HEADLESS）；登录路径不传、恒为
    headed——人工扫码 / 登录必须在 noVNC 里看得见实时画面。
    """
    options: dict[str, Any] = {
        "headless": headless,
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


def list_accounts(
    db: Session,
    q: str | None = None,
    *,
    viewer_id: int | None = None,
    role: str = "operator",
) -> list[Account]:
    """账号列表。``q`` 为可选泛搜索关键词：对 账号名称 / 备注 / 联系方式(手机号) 三字段做
    不区分大小写的包含匹配（任一命中即返回）。空白 q 视为不过滤。

    可见性（共享账号，见设计稿 §6）：
      - 永远排除 merged_into 非空的被并入行（不出现在任何用户的列表）。
      - admin（role == "admin"）见全部；否则只见 owner ∪ 成员账号（viewer_id 必传）。
      - viewer_id 为 None 且非 admin 视为「无可见账号」，返回空（防御性，不应发生）。
    """
    stmt = (
        select(Account)
        .where(
            Account.is_deleted == False,  # noqa: E712
            Account.merged_into.is_(None),
        )
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
    if role != "admin":
        if viewer_id is None:
            return []
        member_subq = (
            select(AccountMember.account_id)
            .where(AccountMember.user_id == viewer_id)
            .scalar_subquery()
        )
        stmt = stmt.where(or_(Account.user_id == viewer_id, Account.id.in_(member_subq)))
    return list(db.execute(stmt).scalars().all())


def get_account(db: Session, account_id: int) -> Account | None:
    stmt = (
        select(Account)
        .where(Account.id == account_id, Account.is_deleted == False)  # noqa: E712
        .options(selectinload(Account.platform))
    )
    return db.execute(stmt).scalar_one_or_none()


def is_api_platform_code(code: str) -> bool:
    """是否"凭据直填"型平台（前端据此走 AppID/Secret 直填、而非浏览器扫码登录）。

    注意与 is_api_driver 的区别：is_api_driver 说的是**发布**走服务端 API（不起浏览器）；
    本函数说的是**建号/登录**走凭据直填。二者多数情况一致（公众号），但 cookie-session 型
    API 驱动（TapTap：API 发布但靠浏览器登录拿 cookie）发布=api、登录=browser，故排除之。
    """
    from server.app.modules.tasks.drivers import get_driver, is_api_driver

    if code in _API_PLATFORM_CODES:
        return True
    if not is_api_driver(code):
        return False
    try:
        return getattr(get_driver(code), "auth", "token") != "cookie"
    except Exception:
        return True


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


def set_taptap_forum(db: Session, account: Account, payload: TaptapForumIn) -> Account:
    """设置 TapTap 账号论坛绑定（app_id/group_id/x_ua）。x_ua 未传则保留原值（不清空）。

    与 wechat 凭据路径分开：TapTap 无 app_secret、走 cookie 登录，api_credentials 语义不同。
    """
    if account.platform.code != "taptap":
        raise ValidationError("仅 TapTap 账号支持论坛绑定配置")
    creds = dict(account.api_credentials or {})
    creds["app_id"] = payload.app_id
    creds["group_id"] = payload.group_id
    if payload.x_ua is not None:
        creds["x_ua"] = payload.x_ua
    account.api_credentials = creds
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

    # 清空成员行（共享账号软删时随之失访，见设计稿 §6「删除语义」）。账号走软删故 FK CASCADE
    # 平时不触发，这里手动 DELETE。
    db.execute(delete(AccountMember).where(AccountMember.account_id == account_id))

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

    changed = False

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
            changed = True

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
        changed = True

    # 3) 审计：仅本次调用产生了实际状态变更时才写（no-op 重调不追加审计行）
    if changed:
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


# ── AccountRead 派生字段辅助（owner_name / member_count，见设计稿 §2.6）─────────


def count_account_members(db: Session, account_id: int) -> int:
    """该 canonical 账号的成员数（不含 owner）。"""
    from sqlalchemy import func

    return int(
        db.execute(
            select(func.count())
            .select_from(AccountMember)
            .where(AccountMember.account_id == account_id)
        ).scalar_one()
    )


def member_counts_for(db: Session, account_ids: list[int]) -> dict[int, int]:
    """批量查多个账号的成员数，避免列表 N+1。返回 {account_id: count}（缺省 0）。"""
    if not account_ids:
        return {}
    from sqlalchemy import func

    rows = db.execute(
        select(AccountMember.account_id, func.count())
        .where(AccountMember.account_id.in_(account_ids))
        .group_by(AccountMember.account_id)
    ).all()
    return {int(aid): int(cnt) for aid, cnt in rows}


def owner_names_for(db: Session, owner_ids: list[int]) -> dict[int, str | None]:
    """批量查 owner 用户的显示名（display_name 优先，回落 username）。返回 {user_id: name}。"""
    ids = list({int(i) for i in owner_ids})
    if not ids:
        return {}
    rows = db.execute(
        select(User.id, User.display_name, User.username).where(User.id.in_(ids))
    ).all()
    return {int(uid): (dn or un) for uid, dn, un in rows}


# ── 成员管理（owner/admin 可见 + 移除，见设计稿 §6「成员管理」）──────────────


def list_account_members(db: Session, account: Account) -> list[dict[str, Any]]:
    """列出账号成员（含 owner 行）。owner 排首位、is_owner=True、granted_via=None。

    返回纯 dict 列表，router 负责序列化成 AccountMemberRead。
    """
    rows: list[dict[str, Any]] = []

    owner = db.get(User, account.user_id)
    rows.append(
        {
            "account_id": account.id,
            "user_id": account.user_id,
            "username": owner.username if owner is not None else None,
            "display_name": owner.display_name if owner is not None else None,
            "is_owner": True,
            "granted_via": None,
            "created_at": account.created_at,
        }
    )

    members = (
        db.execute(
            select(AccountMember)
            .where(AccountMember.account_id == account.id)
            .order_by(AccountMember.created_at.asc(), AccountMember.user_id.asc())
        )
        .scalars()
        .all()
    )
    member_user_ids = [m.user_id for m in members]
    users = (
        {
            u.id: u
            for u in db.execute(select(User).where(User.id.in_(member_user_ids))).scalars().all()
        }
        if member_user_ids
        else {}
    )
    for m in members:
        u = users.get(m.user_id)
        rows.append(
            {
                "account_id": m.account_id,
                "user_id": m.user_id,
                "username": u.username if u is not None else None,
                "display_name": u.display_name if u is not None else None,
                "is_owner": False,
                "granted_via": m.granted_via,
                "created_at": m.created_at,
            }
        )
    return rows


def remove_account_member(db: Session, account: Account, user_id: int) -> bool:
    """移除一个成员（幂等）。返回是否实际删除了一行。

    不允许移除 owner（owner 不在成员表，传 owner id 时静默无操作并返回 False）。
    """
    if user_id == account.user_id:
        # owner 不是成员、不可经此移除；幂等地返回未变更。
        return False
    existing = db.execute(
        select(AccountMember.user_id).where(
            AccountMember.account_id == account.id,
            AccountMember.user_id == user_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        return False
    db.execute(
        delete(AccountMember).where(
            AccountMember.account_id == account.id,
            AccountMember.user_id == user_id,
        )
    )
    db.flush()
    return True


# ── admin 批量回填 creator-ID（设计稿 §5 触发点 B）──────────────────────────


def backfill_identity_for_accounts(db: Session) -> dict[str, int]:
    """扫描「浏览器平台 + status==valid + platform_user_id IS NULL + merged_into IS NULL」的账号，
    逐个驱动既有检测+抽取+决议引擎（check_account 的 B2 路径）回填 creator-ID / 合并重复。

    返回汇总计数 {processed, backfilled, merged, conflicts, still_unknown, failed}。

    注意：每个账号都真实开浏览器（check_account），生产仅容器内可用；测试须 monkeypatch
    extraction/detection。本函数顺序处理、各账号独立 try/except，单个失败不拖垮整批。
    """
    from server.app.modules.accounts.auth import check_account
    from server.app.modules.accounts.schemas import AccountCheckRequest

    stmt = (
        select(Account)
        .where(
            Account.is_deleted == False,  # noqa: E712
            Account.merged_into.is_(None),
            Account.status == "valid",
            Account.platform_user_id.is_(None),
            Account.state_path.is_not(None),
        )
        .options(selectinload(Account.platform))
    )
    candidates = list(db.execute(stmt).scalars().all())

    summary = {
        "processed": 0,
        "backfilled": 0,
        "merged": 0,
        "conflicts": 0,
        "still_unknown": 0,
        "failed": 0,
    }

    for account in candidates:
        # API 接入账号（无 state_path）已被上面的 state_path IS NOT NULL 过滤掉，
        # 但浏览器平台仍可能在 check 时抛 API 限制——双保险跳过。
        if is_api_platform_code(account.platform.code):
            continue
        summary["processed"] += 1
        account_id = account.id
        try:
            updated = check_account(db, account, AccountCheckRequest())
        except Exception:  # 单账号失败（浏览器/网络/DOM 漂移）不拖垮整批
            db.rollback()
            summary["failed"] += 1
            continue

        # check_account 返回决议后应呈现的 canonical（resolved_id），决议内部自提交。
        # 归类（候选全部 platform_user_id IS NULL，故不会走「已有值 != X」的身份冲突分支，
        # conflicts 恒为 0；保留该计数维持响应契约稳定）：
        #   - resolved.id != account_id        → self 被并入既有 canonical          = merged
        #   - resolved.id == account_id 且已写 X → self 升为 canonical               = backfilled
        #   - 仍 NULL                          → 抽取为空 / 未登录                   = still_unknown
        resolved = updated
        if resolved is not None and resolved.id != account_id:
            summary["merged"] += 1
        elif resolved is not None and resolved.platform_user_id:
            summary["backfilled"] += 1
        else:
            summary["still_unknown"] += 1

    return summary
