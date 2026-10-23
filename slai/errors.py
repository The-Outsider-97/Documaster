"""Typed errors. Every error carries a stable ``code`` the UI can act on and an HTTP status."""
from __future__ import annotations

from typing import Any


class DocumasterError(Exception):
    code = "error"
    status = 400

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class NotFound(DocumasterError):
    code, status = "not_found", 404


class ValidationFailed(DocumasterError):
    code, status = "invalid_request", 400


class UnsupportedOperation(DocumasterError):
    code, status = "unsupported", 422


class FileRejected(DocumasterError):
    """The file is not what it claims to be, is damaged, or breaks a configured limit."""
    code, status = "file_rejected", 422


class LimitExceeded(DocumasterError):
    code, status = "limit_exceeded", 413


class PasswordRequired(DocumasterError):
    code, status = "password_required", 423


class WrongPassword(DocumasterError):
    code, status = "wrong_password", 403


class PermissionDenied(DocumasterError):
    """The document's own permissions forbid this operation."""
    code, status = "permission_denied", 403


class DependencyMissing(DocumasterError):
    code, status = "dependency_missing", 501


class AgentUnavailable(DocumasterError):
    code, status = "agent_unavailable", 503


class Cancelled(DocumasterError):
    code, status = "cancelled", 409


class TimedOut(DocumasterError):
    code, status = "timeout", 504


class Conflict(DocumasterError):
    code, status = "conflict", 409


class OutputFailed(DocumasterError):
    code, status = "output_failed", 500
