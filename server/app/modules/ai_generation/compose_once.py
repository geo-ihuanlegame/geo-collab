"""compose_one — 单次直调生文，不进 pipeline_run / scheme_run。

供 MCP `compose_article` tool 调用：Claude Code Loop 想要"现在就给我生一篇"，
不需要走整套编排（无并发闸、无快照、无 retry）。直接复用 article_writer.generate_article_from_prompt。

设计约束：
- 不直接 import 模块全局，路径化的延迟读 question_item / prompt_template，便于测试 monkeypatch
- 抛 ValueError 让 router 转 400（不抛裸 Exception 走全局 500）
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from server.app.modules.ai_generation.article_writer import generate_article_from_prompt
from server.app.modules.ai_generation.question_bank import question_text_of


@dataclass
class ComposeOnceRequest:
    question_item_id: int
    prompt_template_id: int
    model: str | None = None


def _load_question_item(db: Session, item_id: int) -> Any:
    from server.app.modules.ai_generation.models import QuestionItem

    return db.query(QuestionItem).filter(QuestionItem.id == item_id).first()


def _load_prompt_template(db: Session, tpl_id: int) -> Any:
    from server.app.modules.prompt_templates.models import PromptTemplate

    return db.query(PromptTemplate).filter(PromptTemplate.id == tpl_id).first()


def compose_one(
    *,
    session_factory: Callable[[], Session],
    user_id: int,
    req: ComposeOnceRequest,
) -> int:
    """调底层 article_writer 生一篇并返回 article_id。

    抛 ValueError 表示参数错（router 转 400）；底层 LLM/DB 异常向上抛（router 转 500）。
    """
    # 先用短会话拿 template_content + question_text
    db = session_factory()
    try:
        item = _load_question_item(db, req.question_item_id)
        if item is None:
            raise ValueError(f"question_item not found: id={req.question_item_id}")
        tpl = _load_prompt_template(db, req.prompt_template_id)
        if tpl is None:
            raise ValueError(f"prompt_template not found: id={req.prompt_template_id}")

        question_text = question_text_of(item)
        template_content = tpl.content
    finally:
        if db is not None:
            db.close()

    # article_writer 自带短会话池管理，session_factory 透传过去
    return generate_article_from_prompt(
        session_factory=session_factory,
        user_id=user_id,
        template_content=template_content,
        question_text=question_text,
        model=req.model,
    )
