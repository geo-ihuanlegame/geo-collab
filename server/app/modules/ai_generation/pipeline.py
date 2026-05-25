"""LangGraph 三节点生成管道：planner → parallel_write → finalize。

图结构：
  START → planner → parallel_write → finalize → END

- planner：顺序调用 LiteLLM，读取 Skill 文件，输出 N 份 task_specs
- parallel_write：ThreadPoolExecutor(max_workers=4)，每个 spec 独立写一篇文章
- finalize：更新 GenerationSession 状态为 done/failed
"""
import json
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)


# ── State ────────────────────────────────────────────────────────────────────

class PipelineState(TypedDict):
    session_id: int
    user_id: int
    skill_path: str
    prompt_content: str
    extra_instruction: str
    task_specs: list[dict]
    article_ids: list[int]
    errors: list[str]
    session_factory: Any  # callable() → Session


# ── 节点：planner ─────────────────────────────────────────────────────────────

def _read_skill_files(skill_path: str) -> str:
    """拼接 SKILL.md 和 references/ 下的所有 .md 文件为 system prompt 上下文。"""
    root = Path(skill_path)
    parts: list[str] = []

    skill_md = root / "SKILL.md"
    if skill_md.exists():
        parts.append(f"# SKILL.md\n{skill_md.read_text(encoding='utf-8')}")

    refs_dir = root / "references"
    if refs_dir.is_dir():
        for md_file in sorted(refs_dir.glob("**/*.md")):
            parts.append(f"# {md_file.name}\n{md_file.read_text(encoding='utf-8')}")

    skeletons_dir = root / "skeletons"
    if skeletons_dir.is_dir():
        for md_file in sorted(skeletons_dir.glob("**/*.md")):
            parts.append(f"# skeleton:{md_file.name}\n{md_file.read_text(encoding='utf-8')}")

    return "\n\n---\n\n".join(parts)


def planner_node(state: PipelineState) -> dict:
    import litellm
    from server.app.core.config import get_settings

    settings = get_settings()
    _inject_api_key(settings.ai_api_key)

    skill_context = _read_skill_files(state["skill_path"])

    system_prompt = (
        "你是一位专业的内容规划师。根据下方的技能文档（SKILL）和用户的写作指令（Prompt），"
        "规划出一批独立的文章写作任务。\n\n"
        "## 技能文档\n\n" + skill_context
    )

    user_prompt = (
        f"## 写作指令\n\n{state['prompt_content']}\n\n"
        + (f"## 补充说明\n\n{state['extra_instruction']}\n\n" if state["extra_instruction"] else "")
        + "请输出一个 JSON 数组，每个元素代表一篇文章的写作任务规格，字段：\n"
        '{"title": "文章标题", "topic": "核心主题", "angle": "写作角度", "skeleton_hint": "骨架提示"}\n\n'
        "只输出 JSON，不要有其他文字。"
    )

    response = litellm.completion(
        model=settings.ai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        api_key=settings.ai_api_key or None,
    )
    raw = response.choices[0].message.content or "[]"

    # 提取 JSON（兼容 AI 在代码块里包裹的情况）
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    try:
        task_specs = json.loads(raw.strip())
        if not isinstance(task_specs, list):
            logger.warning("Planner output is not a list: %s", raw[:200])
            task_specs = []
    except json.JSONDecodeError:
        logger.warning("Planner output is not valid JSON: %s", raw[:200])
        task_specs = []

    errors: list[str] = []
    if not task_specs:
        errors = ["规划阶段未产出有效任务规格（LLM 输出为空或格式无效）"]

    return {"task_specs": task_specs, "errors": errors}


# ── 节点：parallel_write ──────────────────────────────────────────────────────

def _write_one_article(
    spec: dict,
    user_id: int,
    skill_path: str,
    session_factory: Any,
) -> int | None:
    """在独立线程中生成一篇文章并写库，返回 article_id 或 None（失败）。"""
    import litellm
    from server.app.core.config import get_settings
    from server.app.modules.ai_generation.converter import markdown_to_html, markdown_to_tiptap
    from server.app.modules.articles.service import create_article
    from server.app.modules.articles.schemas import ArticleCreate

    settings = get_settings()
    _inject_api_key(settings.ai_api_key)

    skill_context = _read_skill_files(skill_path)
    system_prompt = (
        "你是一位专业的内容写作者。根据下方的技能文档和写作任务规格，"
        "撰写一篇高质量的文章。使用 Markdown 格式输出正文，包含标题（# 一级标题）。\n\n"
        "## 技能文档\n\n" + skill_context
    )
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
                state["skill_path"],
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
    error_message = "; ".join(errors) if errors else None

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


def run_pipeline(db: Any, session_id: int, *, session_factory: Any) -> None:
    """由后台线程调用；db 仅用于读取会话元数据和设置 running 状态。"""
    from server.app.modules.ai_generation.service import get_session, update_session_status
    from server.app.modules.skills.service import get_skill
    from server.app.modules.prompt_templates.models import PromptTemplate

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
        update_session_status(db, session_id, status="failed", error_message="Skill 或 Prompt 不存在")
        db.commit()
        return

    update_session_status(db, session_id, status="running")
    db.commit()

    try:
        _graph.invoke(
            {
                "session_id": session_id,
                "user_id": gen_session.user_id,
                "skill_path": skill.storage_path,
                "prompt_content": prompt.content,
                "extra_instruction": gen_session.extra_instruction or "",
                "task_specs": [],
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
