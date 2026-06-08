"""
系统级 ORM 模型：跨域共用的基础实体。

包含：
  - User          — 平台用户（认证、权限）
  - Platform      — 发布平台（toutiao 等，种子数据）
  - WorkerHeartbeat — 后台 Worker 心跳注册
"""

from datetime import datetime

import bcrypt
from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.app.core.time import utcnow
from server.app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="operator")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    feishu_open_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    solo_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ai_format_preset_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def set_password(self, raw: str) -> None:
        """校验长度（<8 抛 ValueError）后用 bcrypt 加盐哈希写入 password_hash。"""
        if len(raw) < 8:
            raise ValueError("密码长度不能少于 8 位")
        self.password_hash = bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    def check_password(self, raw: str) -> bool:
        """bcrypt 校验明文密码是否匹配已存哈希。"""
        return bcrypt.checkpw(raw.encode("utf-8"), self.password_hash.encode("utf-8"))


class Platform(Base):
    __tablename__ = "platforms"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)  # 平台编码，如 toutiao
    name: Mapped[str] = mapped_column(String(100))  # 显示名称
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    accounts = relationship("Account", back_populates="platform")
    publish_tasks = relationship("PublishTask", back_populates="platform")


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
