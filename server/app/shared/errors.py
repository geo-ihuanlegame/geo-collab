"""客户端可见异常族（HTTP 4xx）。

service 层抛这些命名异常，由 main.create_app 的全局处理器映射到对应状态码：
ConflictError→409、ValidationError/AccountError→400、ClientError→400。
注意：没有针对裸 ValueError 的兜底，service 层不要抛 ValueError。
"""


class ClientError(Exception):
    """Base for all client-visible errors (HTTP 4xx). Use instead of ValueError in service code."""


class ConflictError(ClientError):
    """Raised when an optimistic version or idempotency conflict is detected. (HTTP 409)"""


class ValidationError(ClientError):
    """Raised when user input validation fails. (HTTP 400)"""


class AccountError(ClientError):
    """Raised for account-related errors (expired, not found, platform mismatch). (HTTP 400)"""
