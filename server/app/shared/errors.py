class ClientError(Exception):
    """Base for all client-visible errors (HTTP 4xx). Use instead of ValueError in service code."""


class ConflictError(ClientError):
    """Raised when an optimistic version or idempotency conflict is detected. (HTTP 409)"""


class ValidationError(ClientError):
    """Raised when user input validation fails. (HTTP 400)"""


class AccountError(ClientError):
    """Raised for account-related errors (expired, not found, platform mismatch). (HTTP 400)"""
