"""方案运行 executor：展开 run tasks → 并发执行 → 汇总 run 状态。

设计要点（对照可行性分析 Q3/Q4）：
- 三段式：`create_run`（展开快照→pending tasks）/ `run_scheme`（并发执行+汇总）/ 纯函数易测。
- 每篇文章一条 task，互不依赖（embarrassingly parallel），用 ThreadPoolExecutor(max_workers=4)。
- 每个并行 worker 自带独立 DB session（session 非线程安全）。
- 运行时每条 task 从该问题类型允许列表随机抽一个**有效**模板（不存在/停用/删除/非 generation
  都视为无效）；若整列无效 → 该 task 失败、其他类型继续。
- 只读方案快照（question_text），不碰 QuestionItem.status/article_id。
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from server.app.core.time import utcnow
from server.app.modules.ai_generation.article_writer import generate_article_from_prompt
from server.app.modules.ai_generation.models import (
    GenerationScheme,
    GenerationSchemeRun,
    GenerationSchemeRunTask,
)
from server.app.modules.ai_generation.scheme_service import get_line_questions, get_lines
from server.app.modules.articles.ai_format import all_category_contexts, run_ai_format
from server.app.modules.prompt_templates.service import get_visible_prompt_template

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Any]


def _render_questions(questions: list[Any]) -> str:
    """把方案行的问题快照渲染成编号问题列表。"""
    lines: list[str] = []
    for q in questions:
        text = (q.question_text or "").strip()
        if text:
            lines.append(text)
    return "\n".join(f"{i}. {t}" for i, t in enumerate(lines, start=1))


def create_run(db: Any, *, scheme: GenerationScheme, user_id: int) -> GenerationSchemeRun:
    """按方案行展开 run tasks（每行 article_count 条）。使用方案快照，不读问题池最新文本。"""
    run = GenerationSchemeRun(
        scheme_id=scheme.id,
        user_id=user_id,
        status="pending",
        article_ids=[],
        ai_engine=scheme.ai_engine,
    )
    db.add(run)
    db.flush()

    for line in get_lines(db, scheme.id):
        questions = get_line_questions(db, line.id)
        question_text = _render_questions(questions)
        item_ids = [q.question_item_id for q in questions if q.question_item_id is not None]
        allowed = list(line.allowed_prompt_template_ids or [])
        for _ in range(max(0, int(line.article_count or 0))):
            db.add(
                GenerationSchemeRunTask(
                    run_id=run.id,
                    scheme_line_id=line.id,
                    question_type=line.question_type,
                    question_text=question_text,
                    question_item_ids=item_ids,
                    allowed_prompt_template_ids=allowed,
                    status="pending",
                )
            )
    db.flush()
    return run


def _pick_valid_template(
    db: Any, allowed_ids: list[int], user_id: int, *, rng: random.Random | None = None
) -> Any:
    """从允许列表里筛出运行时有效的模板（可见/未删/启用/scope=generation），随机返回一个；全无效返回 None。"""
    valid = []
    for tid in dict.fromkeys(allowed_ids or []):
        tpl = get_visible_prompt_template(db, tid, user_id=user_id, scope="generation")
        if tpl is not None and tpl.is_enabled:
            valid.append(tpl)
    if not valid:
        return None
    rng = rng or random.Random()
    return rng.choice(valid)


def _fail_task(db: Any, task_id: int, message: str) -> None:
    task = db.get(GenerationSchemeRunTask, task_id)
    if task is None:
        return
    task.status = "failed"
    task.error_message = message[:1000]
    task.completed_at = utcnow()
    db.commit()


def _execute_task(
    task_id: int,
    user_id: int,
    session_factory: SessionFactory,
    model_override: str | None = None,
) -> int | None:
    """执行一条 task：选模板 → 生文 → 写结果。返回 article_id 或 None（失败）。

    model_override 为方案级 AI 引擎（None / 空 = 用系统默认写作模型）。
    """
    db = session_factory()
    try:
        task = db.get(GenerationSchemeRunTask, task_id)
        if task is None:
            return None
        task.status = "running"
        allowed = list(task.allowed_prompt_template_ids or [])
        question_text = task.question_text or ""
        db.commit()
    finally:
        db.close()

    # 选模板（运行时复核 + 随机），记 actual_prompt_template_id
    db = session_factory()
    try:
        tpl = _pick_valid_template(db, allowed, user_id)
        if tpl is None:
            _fail_task(
                db,
                task_id,
                "该问题类型的允许模板在运行时全部无效（不存在/停用/删除/非 generation）",
            )
            return None
        template_content = tpl.content
        task = db.get(GenerationSchemeRunTask, task_id)
        task.actual_prompt_template_id = tpl.id
        db.commit()
    finally:
        db.close()

    # 生文
    try:
        article_id = generate_article_from_prompt(
            session_factory=session_factory,
            user_id=user_id,
            template_content=template_content,
            question_text=question_text,
            model=model_override,
        )
    except Exception as exc:  # noqa: BLE001 — 单 task 失败隔离
        logger.exception("scheme run task %s generation failed", task_id)
        db = session_factory()
        try:
            _fail_task(db, task_id, str(exc))
        finally:
            db.close()
        return None

    db = session_factory()
    try:
        task = db.get(GenerationSchemeRunTask, task_id)
        task.status = "done"
        task.article_id = article_id
        task.completed_at = utcnow()
        db.commit()
    finally:
        db.close()

    # 生文成功后自动 AI 排版 + 全 bucket 智能配图（best-effort，不影响 task 结果）
    _auto_format_article(article_id, user_id, session_factory)
    # [临时] 封面兜底：从 cantingyangchengji bucket 随机取图当封面（后期删除本行 + _assign_temp_cover_from_bucket）
    _assign_temp_cover_from_bucket(article_id, user_id, session_factory)
    return article_id


def run_scheme(run_id: int, session_factory: SessionFactory) -> None:
    """方案运行入口（由后台线程调用）：并发执行所有 task，汇总 run 状态。"""
    db = session_factory()
    try:
        run = db.get(GenerationSchemeRun, run_id)
        if run is None:
            logger.error("run_scheme: run %s not found", run_id)
            return
        run.status = "running"
        user_id = run.user_id
        model_override = run.ai_engine
        task_ids = [
            t.id
            for t in db.query(GenerationSchemeRunTask)
            .filter(GenerationSchemeRunTask.run_id == run_id)
            .order_by(GenerationSchemeRunTask.id.asc())
            .all()
        ]
        db.commit()
    finally:
        db.close()

    if task_ids:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(_execute_task, tid, user_id, session_factory, model_override): tid
                for tid in task_ids
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    logger.exception("scheme run task %s crashed", futures[future])

    _aggregate_run(run_id, session_factory)
    _group_run_articles(run_id, session_factory)


def recover_stuck_scheme_runs(db: Any) -> None:
    """启动时复位崩溃残留的方案运行（running/pending → failed）。

    与 pipeline run 对称：进程刚启动时没有 run 真正在执行，所有 running/pending 都是
    上次崩溃的残留，直接置 failed（方案运行没有租约机制，故全量复位、不按阈值）。
    """
    from sqlalchemy import select

    runs = list(
        db.execute(
            select(GenerationSchemeRun).where(
                GenerationSchemeRun.status.in_(("running", "pending"))
            )
        )
        .scalars()
        .all()
    )
    for run in runs:
        run.status = "failed"
        run.error_message = "进程重启：运行在上次执行中意外中断"
        run.completed_at = utcnow()
    if runs:
        logger.warning("Recovered %d stuck scheme runs: %s", len(runs), [r.id for r in runs])
        db.commit()


def _aggregate_run(run_id: int, session_factory: SessionFactory) -> None:
    db = session_factory()
    try:
        run = db.get(GenerationSchemeRun, run_id)
        if run is None:
            return
        tasks = (
            db.query(GenerationSchemeRunTask).filter(GenerationSchemeRunTask.run_id == run_id).all()
        )
        done = [t for t in tasks if t.status == "done"]
        failed = [t for t in tasks if t.status == "failed"]
        run.article_ids = [t.article_id for t in done if t.article_id is not None]
        if not done:
            run.status = "failed"
        elif failed:
            run.status = "partial_failed"
        else:
            run.status = "done"
        if failed:
            run.error_message = "; ".join(
                f"task#{t.id}: {t.error_message or '失败'}" for t in failed
            )[:2000]
        run.completed_at = utcnow()
        db.commit()
    finally:
        db.close()


def _group_run_articles(run_id: int, session_factory: SessionFactory) -> None:
    """方案运行产出文章：标 pending + 归入新方案组。复用 articles.mark_pending_and_group。"""
    from server.app.modules.articles.service import mark_pending_and_group

    db = session_factory()
    try:
        run = db.get(GenerationSchemeRun, run_id)
        if run is None:
            return
        article_ids = list(run.article_ids or [])
        if not article_ids:
            return
        scheme = db.get(GenerationScheme, run.scheme_id)
        scheme_name = scheme.name if scheme is not None else f"方案 {run.scheme_id}"
        base_name = f"{run.created_at:%Y/%m/%d %H:%M} · {scheme_name}"
        uid = run.user_id
        rid = run.id
    finally:
        db.close()

    mark_pending_and_group(
        session_factory,
        article_ids=article_ids,
        user_id=uid,
        base_name=base_name,
        fallback_suffix=f"#{rid}",
    )


def _auto_format_article(
    article_id: int,
    user_id: int,
    session_factory: SessionFactory,
) -> None:
    """方案生文成功后自动 AI 排版 + 用全部图片 bucket 智能配图。

    best-effort：任何失败只记日志，绝不影响已生成的文章 / task 状态。
    """
    try:
        from server.app.modules.articles.ai_format import has_ai_format_targets
        from server.app.modules.articles.models import Article
        from server.app.modules.system.models import User

        lock_started_at = utcnow().replace(microsecond=0)
        preset_id: int | None = None
        candidate_categories: list[Any] = []

        db = session_factory()
        try:
            article = db.get(Article, article_id)
            if article is None or article.is_deleted:
                return
            if not has_ai_format_targets(article.content_json):
                return
            user = db.get(User, user_id)
            preset_id = getattr(user, "ai_format_preset_id", None) if user else None
            candidate_categories = all_category_contexts(db)
            article.ai_checking = True
            article.ai_checking_started_at = lock_started_at
            article.ai_format_error = None
            db.commit()
        finally:
            db.close()

        run_ai_format(
            article_id,
            include_images=True,
            lock_started_at=lock_started_at,
            preset_id=preset_id,
            user_id=user_id,
            candidate_categories=candidate_categories,
        )
    except Exception:  # noqa: BLE001 — 自动排版失败不影响生文结果
        logger.exception("auto ai_format failed for article %s", article_id)


# ─────────────────────────────────────────────────────────────────────────────
# [临时] 封面兜底
#
# 封面图目前没有自动生成逻辑。在接入真正的封面方案之前，先从 cantingyangchengji
# （餐厅养成记）bucket 随机取一张图当封面。后期删除整段（常量 + 函数 + _execute_task
# 里的调用）即可，不影响其它逻辑。
# ─────────────────────────────────────────────────────────────────────────────
_TEMP_COVER_BUCKET = "cantingyangchengji"


def _assign_temp_cover_from_bucket(
    article_id: int,
    user_id: int,
    session_factory: SessionFactory,
) -> None:
    """[临时] 从 cantingyangchengji bucket 随机取一张图，落成 Asset 后设为文章封面。

    - 走 store_bytes 落成真实 Asset（cover_asset_id 外键有效，发布驱动可上传文件）。
    - 已有封面的文章跳过，不覆盖。
    - best-effort：任何失败只记日志，绝不影响已生成的文章 / task 状态。
    """
    try:
        import mimetypes

        from sqlalchemy import func

        from server.app.modules.articles.models import Article
        from server.app.modules.articles.store import store_bytes
        from server.app.modules.image_library.models import StockCategory, StockImage
        from server.app.modules.image_library.store import get_object_bytes

        db = session_factory()
        try:
            article = db.get(Article, article_id)
            if article is None or article.is_deleted or article.cover_asset_id:
                return

            category = (
                db.query(StockCategory)
                .filter(StockCategory.bucket_name == _TEMP_COVER_BUCKET)
                .first()
            )
            if category is None:
                logger.info("temp cover: bucket %s 未注册，跳过", _TEMP_COVER_BUCKET)
                return

            image = (
                db.query(StockImage)
                .filter(StockImage.category_id == category.id)
                .order_by(func.rand())
                .first()
            )
            if image is None:
                logger.info("temp cover: bucket %s 无图片，跳过", _TEMP_COVER_BUCKET)
                return

            data = get_object_bytes(category.bucket_name, image.minio_key)
            content_type = mimetypes.guess_type(image.filename)[0] or "image/jpeg"
            stored = store_bytes(db, user_id, data, image.filename, content_type)

            article.cover_asset_id = stored.asset.id
            article.version += 1
            article.updated_at = utcnow()
            db.commit()
            logger.info("temp cover 设置成功 article=%s image=%s", article_id, image.minio_key)
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — 临时封面失败不影响生文结果
        logger.exception("temp cover assignment failed for article %s", article_id)
