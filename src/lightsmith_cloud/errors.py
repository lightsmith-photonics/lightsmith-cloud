"""Errors are JSON everywhere: ``{"error": <type>, "message": <text>, ...}``."""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    def __init__(self, status: int, error: str, message: str, *, headers: dict[str, str] | None = None,
                 **extra: Any):
        super().__init__(message)
        self.status, self.error, self.message, self.headers = status, error, message, headers
        self.extra = {k: v for k, v in extra.items() if v is not None}

    def body(self) -> dict[str, Any]:
        return {"error": self.error, "message": self.message, **self.extra}


def not_found(what: str = "job") -> ApiError:
    return ApiError(404, "NotFound", f"{what} not found")
