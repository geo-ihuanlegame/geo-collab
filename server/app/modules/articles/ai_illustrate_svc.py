"""ai_illustrate_svc —— 单篇文章「AI 智能配图 + 自动封面」的共享 service.

被 pipelines/nodes/ai_illustrate.py 和 articles MCP endpoint 共用，
保证两条路径配图效果完全一致.

不并发（单文章），调用方按需自管 ThreadPoolExecutor 包多篇.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from server.app.core.time import utcnow
from server.app.modules.articles.ai_format import (
    category_contexts_for,
    has_ai_format_targets,
    run_ai_format,
    run_ai_format_from_game_list,
)
from server.app.modules.articles.models import Article
from server.app.modules.image_library.cover import (
    CoverResult,
    set_random_cover_from_category,
)
from server.app.modules.image_library.fallback import apply_image_fallback

_logger = logging.getLogger(__name__)


@dataclass
class IllustrateOptions:
    """配图旋钮，跟 pipeline ai_illustrate 节点的 cfg 字段一一对应.

    max_images / min_spacing 的 0 等同 None（视为未设置，回退到风格默认 12/1 或 3/5）；
    要"无上限"请用 None；想要硬上限则传正整数。
    """

    include_companion: bool = True
    web_fallback: bool = False
    aggressive_images: bool = True
    max_images: int | None = None
    min_spacing: int | None = None
    preset_id: int | None = None
    set_cover: bool = True
    # 配图模型（litellm 模型串，scope=ai_format）；None/"" = 走默认格式模型
    format_model: str | None = None
    # 上游识别分支传下来的显式游戏清单；None=回退现有模型识别路径
    game_list: list[dict] | None = None


_ILLUSTRATION_SKIP_PREFIX = "[illustration_skip] "


@dataclass
class IllustrateResult:
    article_id: int
    images_inserted: int = 0
    cover_status: str = "skipped"
    cover_error: str | None = None
    format_error: str | None = None
    # warning: AI / 数据决策为空导致 0 张图（无 throw 异常），区别于 format_error。
    # 比如 AI 返了空 image_positions、栏目里无图等——这是合法分支但 writer / 飞书要可见。
    warning: str | None = None
    # 部分配图诊断（来自 run_ai_format 的 out_diagnostics）：requested=应配位置数、
    # missed=没配上的张数、missed_games=没配上的游戏名/栏目。inserted < requested 时
    # warning 会带 partial_images 文案，避免"该 N 张只来 M 张"被静默当成功（见 #部分配图盲区）。
    requested: int = 0
    missed: int = 0
    missed_games: list[str] = field(default_factory=list)
    fallback_inserted: int = 0


def _resolve_illustration_outcome(
    *,
    raw_error: str | None,
    images_inserted: int,
    fmt_diag: dict,
) -> tuple[str | None, str | None, int, int, list[str]]:
    """从 run_ai_format 的 raw ai_format_error + 实插图数 + 诊断 dict 推导对外信号。

    返回 (format_error, warning, requested, missed, missed_games)：
    - [illustration_skip] 前缀 = 0 图的"AI 决策为空"信号 → 转 warning（非真 error）。
    - 部分配图失败（inserted>0 但 < requested）→ 合成 partial_images warning，让 writer
      看到"该 N 张只来 M 张"，而不是 inserted 非 0 就静默当成功。0 图全 miss 已被上面的
      skip warning 覆盖，这里不重复报（优先级：skip/error > partial）。
    """
    requested = int(fmt_diag.get("requested", 0) or 0)
    inserted = int(fmt_diag.get("inserted", images_inserted or 0) or 0)
    missed = int(fmt_diag.get("missed", 0) or 0)
    missed_games = list(fmt_diag.get("missed_games", []) or [])

    format_error: str | None = raw_error
    warning: str | None = None
    if raw_error and raw_error.startswith(_ILLUSTRATION_SKIP_PREFIX):
        # ai_format._maybe_insert_images 给出的 "AI 决策为空" 信号 —— 不算 error
        warning = raw_error[len(_ILLUSTRATION_SKIP_PREFIX) :]
        format_error = None
    elif (images_inserted or 0) == 0 and raw_error is None:
        # 兜底：0 张图、无 error、无 skip_reason（防止未来 ai_format 改回不写前缀，
        # writer 仍能拿到一个明确信号）
        warning = "no images inserted (unknown reason — check server log)"

    # 部分配图失败：该配 requested 张、只来 inserted 张。仅在没有更高优先级的 skip/error
    # warning 时报（专抓 inserted>0 但 < requested 的静默盲区）。
    if warning is None and format_error is None and missed > 0:
        games = ("：" + "、".join(missed_games)) if missed_games else ""
        warning = f"partial_images: 应配 {requested} 张，实配 {inserted} 张，缺 {missed} 张{games}"

    return format_error, warning, requested, missed, missed_games


def illustrate_one(
    *,
    article_id: int,
    main_category_id: int,
    user_id: int,
    options: IllustrateOptions,
    session_factory: Callable[[], Session],
) -> IllustrateResult:
    """给一篇文章配图 + 设封面，复用 pipeline ai_illustrate 节点的成熟逻辑.

    session_factory 而非 db：开两个独立短 session（配图持锁 / 封面独立提交），
    跟节点里的 _format_one + _maybe_set_cover 等价.

    异常传播：run_ai_format 内部捕获自身异常并写到 article.ai_format_error，
    正常情况下不抛出. 但若 run_ai_format 仍意外上抛（如 SDK 级 BaseException
    或新代码路径未覆盖的异常），本函数**不捕获**——异常会直接上抛到调用方，
    跳过阶段 2 (封面) 和阶段 3 (回读). 因此 HTTP endpoint / MCP tool 必须自己
    try/except 兜底，转成结构化的 IllustrateResult(format_error=str(exc)) 或
    500 响应，避免直接把内部 traceback 暴露给 LLM-facing 客户端.
    """
    aggressive = options.aggressive_images
    builtin_variant = "aggressive" if aggressive else "conservative"
    max_images = (
        options.max_images
        if (options.max_images and options.max_images > 0)
        else (12 if aggressive else 3)
    )
    min_spacing = (
        options.min_spacing
        if (options.min_spacing and options.min_spacing > 0)
        else (1 if aggressive else 5)
    )

    # 阶段 1: 配图 (持锁 + run_ai_format)
    lock_started_at = utcnow().replace(microsecond=0)
    candidate_categories: list = []
    stamped_game_list: list | None = None

    db = session_factory()
    try:
        article = db.get(Article, article_id)
        if article is None or article.is_deleted:
            return IllustrateResult(
                article_id=article_id, format_error="article not found or deleted"
            )
        if not has_ai_format_targets(article.content_json):
            return IllustrateResult(
                article_id=article_id, format_error="no ai_format_targets in content"
            )
        # web 生文写作模型盖的显式游戏清单（缺省路径的 stamp 兜底，显式 options 优先）
        stamped_game_list = (article.metrics or {}).get("game_positions")
        candidate_categories = category_contexts_for(
            db,
            main_category_id=main_category_id,
            include_companion=options.include_companion,
        )
        article.ai_checking = True
        article.ai_checking_started_at = lock_started_at
        article.ai_format_error = None
        db.commit()
    finally:
        db.close()

    fmt_diag: dict = {}
    # 显式 options.game_list 优先；缺省时回退文章 stamp（web 生文盖的 game_positions）
    effective_game_list = options.game_list if options.game_list is not None else stamped_game_list
    if effective_game_list is not None:
        images_inserted = run_ai_format_from_game_list(
            article_id,
            lock_started_at=lock_started_at,
            game_list=effective_game_list,
            preset_id=options.preset_id,
            user_id=user_id,
            candidate_categories=candidate_categories,
            max_images=max_images,
            min_spacing=min_spacing,
            builtin_variant=builtin_variant,
            out_diagnostics=fmt_diag,
        )
    else:
        images_inserted = run_ai_format(
            article_id,
            include_images=True,
            lock_started_at=lock_started_at,
            preset_id=options.preset_id,
            user_id=user_id,
            candidate_categories=candidate_categories,
            web_fallback=options.web_fallback,
            max_images=max_images,
            min_spacing=min_spacing,
            builtin_variant=builtin_variant,
            format_model_selected=options.format_model,
            out_diagnostics=fmt_diag,
        )

    fallback_inserted = 0
    try:
        # 用 anchored（实际锚定数）而非 requested（作者意图/清单长度）算兜底缺口：
        # 锚定全失败时 anchored=0 → 兜底补 0，绝不灌满随机无关图（见 #1182）。
        anchored = int(fmt_diag.get("anchored", 0) or 0)
        category_ids = [
            c["id"] for c in candidate_categories if isinstance(c, dict) and c.get("id")
        ]
        fallback_inserted = apply_image_fallback(
            article_id=article_id,
            anchored=anchored,
            category_ids=category_ids,
            max_images=max_images,
            session_factory=session_factory,
        )
    except Exception:  # noqa: BLE001
        _logger.exception("fallback random fill failed for article %s", article_id)

    # 阶段 2: 封面 (独立短 session)
    cover_status = "skipped"
    cover_error: str | None = None
    if options.set_cover:
        db = session_factory()
        try:
            article = db.get(Article, article_id)
            if article is not None and not article.is_deleted:
                result: CoverResult = set_random_cover_from_category(
                    db, article, main_category_id, user_id
                )
                db.commit()
                cover_status = result.status
                cover_error = result.error
        except Exception as exc:  # noqa: BLE001 — best-effort
            db.rollback()
            cover_status = "error"
            cover_error = str(exc)
        finally:
            db.close()

    # 阶段 3: 回读 article.ai_format_error，区分 [illustration_skip] 前缀
    raw_error: str | None = None
    db = session_factory()
    try:
        article = db.get(Article, article_id)
        if article is not None:
            raw_error = article.ai_format_error
    finally:
        db.close()

    total_inserted = (images_inserted or 0) + fallback_inserted
    format_error, warning, requested, missed, missed_games = _resolve_illustration_outcome(
        raw_error=raw_error,
        images_inserted=total_inserted,
        fmt_diag=fmt_diag,
    )

    return IllustrateResult(
        article_id=article_id,
        images_inserted=total_inserted,
        cover_status=cover_status,
        cover_error=cover_error,
        format_error=format_error,
        warning=warning,
        requested=requested,
        missed=missed,
        missed_games=missed_games,
        fallback_inserted=fallback_inserted,
    )
