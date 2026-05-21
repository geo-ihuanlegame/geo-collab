import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from server.app.models.generation import GenerationSession


def create_session(
    db: Session,
    *,
    user_id: int,
    skill_id: int,
    prompt_template_id: int,
    extra_instruction: str | None = None,
) -> GenerationSession:
    session = GenerationSession(
        user_id=user_id,
        skill_id=skill_id,
        prompt_template_id=prompt_template_id,
        extra_instruction=extra_instruction,
        status="pending",
        article_ids="[]",
    )
    db.add(session)
    db.flush()
    return session


def get_session(db: Session, session_id: int) -> GenerationSession | None:
    return db.query(GenerationSession).filter(GenerationSession.id == session_id).first()


def update_session_status(
    db: Session,
    session_id: int,
    *,
    status: str,
    article_ids: list[int] | None = None,
    error_message: str | None = None,
) -> None:
    session = db.query(GenerationSession).filter(GenerationSession.id == session_id).first()
    if session is None:
        return
    session.status = status
    if article_ids is not None:
        session.article_ids = json.dumps(article_ids)
    if error_message is not None:
        session.error_message = error_message
    if status in ("done", "failed"):
        session.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.flush()
