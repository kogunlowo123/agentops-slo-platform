"""Domain errors mapped to HTTP status codes and OAuth-style bodies by the API layer."""

from __future__ import annotations


class PlatformError(Exception):
    code = "invalid_request"
    status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(PlatformError):
    code = "not_found"
    status = 404


class ConflictError(PlatformError):
    code = "conflict"
    status = 409


class InvalidStateError(PlatformError):
    code = "invalid_state"
    status = 409


class UnauthorizedError(PlatformError):
    code = "unauthorized"
    status = 401


class ForbiddenError(PlatformError):
    code = "forbidden"
    status = 403


class PayloadTooLargeError(PlatformError):
    code = "payload_too_large"
    status = 413


class UnsupportedMediaTypeError(PlatformError):
    code = "unsupported_media_type"
    status = 415


class UnavailableError(PlatformError):
    """A dependency is down; OTLP exporters retry on 503 with Retry-After."""

    code = "temporarily_unavailable"
    status = 503
