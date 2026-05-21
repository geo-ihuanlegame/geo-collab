from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from server.app.core.time import utcnow
from server.app.db.base import Base


class GenerationSession(Base):
    __tablename__ = "generation_sessions"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending','running','done','failed')",
            name="ck_gen_sessions_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    skill_id: Mapped[int | None] = mapped_column(ForeignKey("skills.id"), nullable=True)
    prompt_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("prompt_templates.id"), nullable=True
    )
    extra_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    article_ids: Mapped[str] = mapped_column(Text, default="[]")  # JSON array of int
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
