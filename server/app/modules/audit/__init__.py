"""审计日志模块：覆盖所有系统级写操作端点的用户行为审计。"""
from server.app.modules.audit.service import add_audit_entry

__all__ = ["add_audit_entry"]
