"""AI 模型注册表服务层：CRUD + 解析器 + 首次播种。

**解析优先级**（写作 / 格式同形）：
1. DB 行：selected 非空→按 model 匹配本 scope 的 enabled 行；selected 空→取本 scope 的
   is_default enabled 行；无匹配则进第 4 步回落。
2. Key：os.environ[api_key_env]（设了且非空）→ scope 全局 key → ""。
3. model 串：row.model or scope 默认；base_url 取 row.base_url。
4. 回落（向后兼容）：无任何 DB 行命中→写作委托 config.resolve_engine（仍认 GEO_AI_ENGINES
   内联 key）；格式回落 settings.ai_format_*。

密钥永不入库：行只存 api_key_env（变量名），运行时从 env 取。
"""

import logging
import os

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.app.core.config import get_settings, resolve_engine
from server.app.modules.ai_models.models import AiModel
from server.app.modules.ai_models.schemas import AiModelCreate, AiModelUpdate
from server.app.shared.errors import ConflictError

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 解析器（被 article_writer / ai_format 调用，须用短生命周期 session）
# --------------------------------------------------------------------------- #
def _resolve_key(api_key_env: str | None, scope_global: str) -> str:
    """行的 api_key_env（环境变量名）→ 实际 key；env 取不到则回落 scope 全局 key。"""
    if api_key_env:
        val = os.environ.get(api_key_env)
        if val:
            return val
    return scope_global or ""


def _match_row(db: Session, *, scope: str, selected: str | None) -> AiModel | None:
    """selected 非空→匹配该 scope 的 enabled 行；空→取该 scope 的 is_default enabled 行。"""
    base = db.query(AiModel).filter(AiModel.scope == scope, AiModel.is_enabled.is_(True))
    sel = (selected or "").strip()
    if sel:
        return base.filter(AiModel.model == sel).first()
    return base.filter(AiModel.is_default.is_(True)).first()


def resolve_writing_engine(db: Session, selected: str | None) -> tuple[str, str, str | None]:
    """写作模型解析 → (model, api_key, base_url)。无 DB 行命中则委托 config.resolve_engine。"""
    settings = get_settings()
    row = _match_row(db, scope="generation", selected=selected)
    if row is not None:
        return (
            row.model or settings.ai_model,
            _resolve_key(row.api_key_env, settings.ai_api_key),
            row.base_url,
        )
    # 回落：env 路径（保留 GEO_AI_ENGINES 内联 key 兼容）
    return resolve_engine(selected)


def resolve_ai_format_model(
    db: Session, selected: str | None = None
) -> tuple[str, str, str | None, int]:
    """Alias for resolve_format_engine; used by auto_review.service and tests."""
    return resolve_format_engine(db, selected=selected)


def resolve_format_engine(
    db: Session, selected: str | None = None
) -> tuple[str, str, str | None, int]:
    """格式/配图模型解析 → (model, api_key, base_url, timeout)。无 DB 行则回落 settings.ai_format_*。"""
    settings = get_settings()
    timeout = settings.ai_format_timeout_seconds
    scope_global = settings.ai_format_api_key or settings.ai_api_key
    row = _match_row(db, scope="ai_format", selected=selected)
    if row is not None:
        return (
            row.model or settings.ai_format_model,
            _resolve_key(row.api_key_env, scope_global),
            row.base_url,
            timeout,
        )
    return settings.ai_format_model, scope_global or "", None, timeout


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def list_models(
    db: Session, *, scope: str | None = None, enabled_only: bool = False
) -> list[AiModel]:
    q = db.query(AiModel)
    if scope is not None:
        q = q.filter(AiModel.scope == scope)
    if enabled_only:
        q = q.filter(AiModel.is_enabled.is_(True))
    return q.order_by(AiModel.scope, AiModel.sort_order, AiModel.id).all()


def get_model(db: Session, model_id: int) -> AiModel | None:
    return db.get(AiModel, model_id)


def _clear_scope_default(db: Session, scope: str, *, exclude_id: int | None = None) -> None:
    """把某 scope 下其它行的 is_default 清掉（配合每 scope 至多一个默认）。"""
    q = db.query(AiModel).filter(AiModel.scope == scope, AiModel.is_default.is_(True))
    if exclude_id is not None:
        q = q.filter(AiModel.id != exclude_id)
    q.update(
        {AiModel.is_default: False, AiModel.is_default_key: None},
        synchronize_session=False,
    )


def _commit_or_conflict(db: Session, action: str) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(f"AI 模型{action}冲突：每个用途至多一个默认模型") from exc


def create_model(db: Session, payload: AiModelCreate) -> AiModel:
    if payload.is_default:
        _clear_scope_default(db, payload.scope)
    row = AiModel(
        label=payload.label,
        model=payload.model,
        scope=payload.scope,
        base_url=payload.base_url,
        api_key_env=payload.api_key_env,
        is_enabled=payload.is_enabled,
        is_default=payload.is_default,
        is_default_key=payload.scope if payload.is_default else None,
        sort_order=payload.sort_order,
    )
    db.add(row)
    _commit_or_conflict(db, "创建")
    db.refresh(row)
    return row


def update_model(db: Session, model_id: int, payload: AiModelUpdate) -> AiModel | None:
    row = get_model(db, model_id)
    if row is None:
        return None
    fields = payload.model_dump(exclude_unset=True)
    new_scope = fields.get("scope", row.scope)
    if fields.get("is_default"):
        _clear_scope_default(db, new_scope, exclude_id=row.id)
    for key, value in fields.items():
        setattr(row, key, value)
    # is_default_key 始终与 (is_default, scope) 同步
    row.is_default_key = row.scope if row.is_default else None
    _commit_or_conflict(db, "更新")
    db.refresh(row)
    return row


def delete_model(db: Session, model_id: int) -> bool:
    row = get_model(db, model_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


# --------------------------------------------------------------------------- #
# 首次播种（启动时调用，幂等）
# --------------------------------------------------------------------------- #
def seed_ai_models_if_empty(db: Session) -> None:
    """表为空时从 settings.ai_engines + 格式默认模型播种；非空即 no-op（幂等）。

    只复制 label/model/base_url；**不写 api_key**（密钥留 env）。带内联 api_key 的 engine
    无法入库，跳过它（仍走 config.resolve_engine 的 env 回落路径），admin 可后续手填 api_key_env。
    """
    if db.query(AiModel.id).first() is not None:
        return
    settings = get_settings()
    seeded_gen = False
    order = 0
    for engine in settings.ai_engines:
        if engine.api_key:
            logger.info(
                "ai_models seed: skip engine %r（内联 api_key 留在 env，未入库）", engine.label
            )
            continue
        is_def = not seeded_gen
        db.add(
            AiModel(
                label=engine.label,
                model=engine.model,
                scope="generation",
                base_url=engine.base_url,
                api_key_env=None,
                is_enabled=True,
                is_default=is_def,
                is_default_key="generation" if is_def else None,
                sort_order=order,
            )
        )
        seeded_gen = True
        order += 1
    db.add(
        AiModel(
            label="默认格式模型",
            model=settings.ai_format_model,
            scope="ai_format",
            base_url=None,
            api_key_env=None,
            is_enabled=True,
            is_default=True,
            is_default_key="ai_format",
            sort_order=0,
        )
    )
    db.commit()
    logger.info("ai_models seeded from env (generation rows + 1 ai_format default)")
