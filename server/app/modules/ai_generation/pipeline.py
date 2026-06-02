"""LangGraph 三节点生成管道：planner → parallel_write → finalize。

图结构：
  START → planner → parallel_write → finalize → END

- planner：当前直接走预构造 specs，节点本身保留作图拓扑占位
- parallel_write：ThreadPoolExecutor(max_workers=4)，每个 spec 独立写一篇文章
- finalize：更新 GenerationSession 状态为 done/failed
"""

import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)


# ── State ────────────────────────────────────────────────────────────────────


class PipelineState(TypedDict):
    session_id: int
    user_id: int
    skill_content: str
    prompt_content: str
    extra_instruction: str
    task_specs: list[dict]
    article_ids: list[int]
    errors: list[str]
    session_factory: Any  # callable() → Session


# ── 节点：planner ─────────────────────────────────────────────────────────────


def planner_node(state: PipelineState) -> dict:
    """已废弃自由规划：task_specs 始终由 run_pipeline 在调用图前预构造好。
    保留节点以维持图拓扑兼容，但内部什么也不做。"""
    return {}


# ── 节点：parallel_write ──────────────────────────────────────────────────────


def _write_one_article(
    spec: dict,
    user_id: int,
    skill_content: str,
    session_factory: Any,
) -> int | None:
    """在独立线程中生成一篇文章并写库，返回 article_id 或 None（失败）。"""
    import litellm

    from server.app.core.config import get_settings
    from server.app.modules.ai_generation.converter import markdown_to_html, markdown_to_tiptap
    from server.app.modules.articles.schemas import ArticleCreate
    from server.app.modules.articles.service import create_article

    settings = get_settings()
    _inject_api_key(settings.ai_api_key)

    system_prompt = (
        "你是一位专业的内容写作者。根据下方的技能文档和写作任务规格，"
        "撰写一篇高质量的文章。使用 Markdown 格式输出正文，包含标题（# 一级标题）。\n\n"
        "## 技能文档\n\n" + skill_content
    )
    if spec.get("user_prompt"):
        # 问题库模式：run_pipeline 已把"基础提示词 + 问题文本"渲染好
        user_prompt = (
            spec["user_prompt"]
            + "\n\n请开始写作（只输出 Markdown 正文，含 # 一级标题，不要解释）："
        )
    else:
        user_prompt = (
            f"## 写作任务\n\n"
            f"标题：{spec.get('title', '无题')}\n"
            f"主题：{spec.get('topic', '')}\n"
            f"角度：{spec.get('angle', '')}\n"
            f"骨架提示：{spec.get('skeleton_hint', '')}\n\n"
            "请开始写作（只输出 Markdown 正文，不要解释）："
        )

    response = litellm.completion(
        model=settings.ai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        api_key=settings.ai_api_key or None,
        timeout=300,
        max_tokens=12000,
    )
    md_content = response.choices[0].message.content or ""

    # 从 Markdown 提取标题
    lines = md_content.strip().splitlines()
    title = spec.get("title", "无题")
    body_lines = lines
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        body_lines = lines[1:]
    body_md = "\n".join(body_lines).strip()

    tiptap_json = markdown_to_tiptap(body_md)
    html = markdown_to_html(body_md)
    plain_text = body_md  # 简化：直接存 Markdown 文本

    article_payload = ArticleCreate(
        title=title,
        content_json=tiptap_json,
        content_html=html,
        plain_text=plain_text,
        word_count=len(plain_text),
        client_request_id=str(uuid.uuid4()),
    )

    db = session_factory()
    try:
        article = create_article(db, user_id, article_payload)
        # 成功才出队/记板块使用 —— 与入库同一事务
        mode = spec.get("mode")
        if mode == "manual" and spec.get("item_ids"):
            from server.app.modules.ai_generation.question_bank import mark_items_consumed

            mark_items_consumed(db, spec["item_ids"], article.id)
        elif mode == "auto" and spec.get("category") and spec.get("pool_id"):
            from server.app.modules.ai_generation.question_bank import mark_category_used

            mark_category_used(db, spec["pool_id"], spec["category"])
        db.commit()
        return article.id
    except Exception:
        db.rollback()
        logger.exception("Failed to save article: %s", spec.get("title"))
        return None
    finally:
        db.close()


def parallel_write_node(state: PipelineState) -> dict:
    article_ids: list[int] = []
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(
                _write_one_article,
                spec,
                state["user_id"],
                state["skill_content"],
                state["session_factory"],
            ): spec
            for spec in state["task_specs"]
        }
        for future in as_completed(futures):
            spec = futures[future]
            try:
                article_id = future.result()
                if article_id is not None:
                    article_ids.append(article_id)
            except Exception as exc:
                errors.append(f"{spec.get('title', '?')}: {exc}")

    return {"article_ids": article_ids, "errors": errors}


# ── 节点：finalize ────────────────────────────────────────────────────────────


def finalize_node(state: PipelineState) -> dict:
    from server.app.modules.ai_generation.service import update_session_status

    article_ids = state["article_ids"]
    errors = state["errors"]
    status = "done" if article_ids else "failed"
    if errors:
        error_message: str | None = "; ".join(errors)
    elif not article_ids:
        # 没有 article、也没有 errors —— 通常是 task_specs 全被静默跳过，给个兜底文案
        error_message = "全部任务都没有产出文章，请检查 Skill / 提示词 / 模型 API key"
    else:
        error_message = None

    db = state["session_factory"]()
    try:
        update_session_status(
            db,
            state["session_id"],
            status=status,
            article_ids=article_ids,
            error_message=error_message,
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("finalize_node: failed to update session status")
    finally:
        db.close()
    return {}


# ── 图构建 ────────────────────────────────────────────────────────────────────


def _build_graph():
    builder: StateGraph = StateGraph(PipelineState)
    builder.add_node("planner", planner_node)
    builder.add_node("parallel_write", parallel_write_node)
    builder.add_node("finalize", finalize_node)
    builder.set_entry_point("planner")
    builder.add_edge("planner", "parallel_write")
    builder.add_edge("parallel_write", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()


_graph = _build_graph()


# ── 公开入口 ──────────────────────────────────────────────────────────────────


def _inject_api_key(api_key: str) -> None:
    if api_key:
        os.environ.setdefault("ANTHROPIC_API_KEY", api_key)
        os.environ.setdefault("OPENAI_API_KEY", api_key)


_QUESTION_PLACEHOLDER = "{{问题}}"


def _render_question_prompt(prompt_content: str, question_text: str) -> str:
    """把问题文本注入基础提示词：有 {{问题}} 占位符则替换，否则追加。"""
    if _QUESTION_PLACEHOLDER in prompt_content:
        return prompt_content.replace(_QUESTION_PLACEHOLDER, question_text)
    return f"{prompt_content}\n\n## 用户问题\n\n{question_text}"


def _build_task_specs(db: Any, gen_session: Any, prompt_content: str) -> list[dict]:
    """构造写作任务列表（单一 pipeline，两种触发方式）：

    - 手动模式（`question_item_ids` 非空）：按 category 分组 → 每组一篇，
      该组所有问题词渲染成编号列表注入 user prompt。
    - 自动模式（`auto_count > 0` + `pool_id`）：按板块优先级轮转选 N 次，
      每次从该板块随机抽 K = randint(1, len(pending)) 条 → 一篇。

    spec 字段：`{title, user_prompt, mode, category, pool_id, item_ids}`。
    """
    import json

    from server.app.modules.ai_generation.question_bank import (
        auto_pick_groups,
        format_question_group,
        get_items,
        group_items_by_category,
    )

    specs: list[dict] = []
    item_ids = json.loads(gen_session.question_item_ids or "[]")

    if item_ids:
        # 手动：把选中的 pending items 按 category 分组合并 → 每组一篇
        selected = [it for it in get_items(db, item_ids) if it.status == "pending"]
        for cat, group in group_items_by_category(selected):
            question_text = format_question_group(group)
            specs.append(
                {
                    "title": "",  # 由正文 # 一级标题推导
                    "mode": "manual",
                    "category": cat,
                    "pool_id": group[0].pool_id if group else None,
                    "item_ids": [it.id for it in group],
                    "user_prompt": _render_question_prompt(prompt_content, question_text),
                }
            )
    elif gen_session.auto_count and gen_session.pool_id:
        # 自动：板块优先级轮转 + 随机抽题 → 每次一篇
        for cat, group in auto_pick_groups(db, gen_session.pool_id, gen_session.auto_count):
            question_text = format_question_group(group)
            specs.append(
                {
                    "title": "",
                    "mode": "auto",
                    "category": cat,
                    "pool_id": gen_session.pool_id,
                    "item_ids": [it.id for it in group],  # 自动模式不消费,仅备查
                    "user_prompt": _render_question_prompt(prompt_content, question_text),
                }
            )
    return specs


def run_pipeline(db: Any, session_id: int, *, session_factory: Any) -> None:
    """由后台线程调用；db 仅用于读取会话元数据和设置 running 状态。"""
    from server.app.modules.ai_generation.service import get_session, update_session_status
    from server.app.modules.prompt_templates.models import PromptTemplate
    from server.app.modules.skills.service import get_skill

    gen_session = get_session(db, session_id)
    if gen_session is None:
        logger.error("run_pipeline: session %d not found", session_id)
        return

    skill = get_skill(db, gen_session.skill_id) if gen_session.skill_id else None
    prompt = (
        db.query(PromptTemplate).filter(PromptTemplate.id == gen_session.prompt_template_id).first()
        if gen_session.prompt_template_id
        else None
    )

    if skill is None or prompt is None:
        update_session_status(
            db, session_id, status="failed", error_message="Skill 或 Prompt 不存在"
        )
        db.commit()
        return

    # 问题库模式：预构造 specs（跳过 planner 自由规划）；否则空数组走 planner。
    prebuilt_specs = _build_task_specs(db, gen_session, prompt.content)

    if not prebuilt_specs:
        # 常见原因：自动模式下池里 pending 全为空或 category 列全 NULL（飞书表没有"分类板块"列）。
        # 不进图，直接写明确错误，避免前端只看到"生成失败，请重试"。
        if gen_session.auto_count and gen_session.pool_id:
            msg = "没有可生成的问题：问题池为空，或飞书表的「分类板块」列没有任何行（自动模式按板块轮转，需要至少一个非空板块）"
        else:
            msg = "没有可生成的问题：选中的问题单元已被消费或为空"
        update_session_status(db, session_id, status="failed", error_message=msg)
        db.commit()
        return

    update_session_status(db, session_id, status="running")
    db.commit()

    try:
        _graph.invoke(
            {
                "session_id": session_id,
                "user_id": gen_session.user_id,
                "skill_content": skill.content or "",
                "prompt_content": prompt.content,
                "extra_instruction": gen_session.extra_instruction or "",
                "task_specs": prebuilt_specs,
                "article_ids": [],
                "errors": [],
                "session_factory": session_factory,
            }
        )
    except Exception as exc:
        fail_db = session_factory()
        try:
            update_session_status(fail_db, session_id, status="failed", error_message=str(exc))
            fail_db.commit()
        except Exception:
            fail_db.rollback()
        finally:
            fail_db.close()
        logger.exception("run_pipeline: pipeline failed for session %d", session_id)
