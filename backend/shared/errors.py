"""API error taxonomy.

Every error the API can return is one of these. The *rendering* of an error into
the API-CONTRACT envelope happens in exactly one place — `shared.responses`.
"""

from __future__ import annotations


class ApiError(Exception):
    """Base class for errors that map onto an API-CONTRACT error code."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ValidationError(ApiError):
    status_code = 400
    code = "validation_error"


class AuthError(ApiError):
    """Missing or invalid credentials. SPEC-DECISIONS D6."""

    status_code = 401
    code = "unauthorized"


class ForbiddenError(ApiError):
    status_code = 403
    code = "forbidden"


class NotFoundError(ApiError):
    status_code = 404
    code = "not_found"


class InternalError(ApiError):
    status_code = 500
    code = "internal_error"
