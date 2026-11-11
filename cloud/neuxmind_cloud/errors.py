"""Uniform error type. Every error response body is ``{"error": "...", "code": "..."}``."""
from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict:
        return {"error": self.message, "code": self.code, **self.extra}


def bad_request(message: str, code: str = "invalid_request") -> ApiError:
    return ApiError(400, code, message)


def unauthorized(message: str = "需要登录或有效的 API Key", code: str = "unauthorized") -> ApiError:
    return ApiError(401, code, message)


def forbidden(message: str = "权限不足", code: str = "forbidden") -> ApiError:
    return ApiError(403, code, message)


def not_found(message: str = "资源不存在", code: str = "not_found") -> ApiError:
    return ApiError(404, code, message)


def conflict(message: str, code: str = "conflict") -> ApiError:
    return ApiError(409, code, message)


def insufficient_credits(available: int, needed: int) -> ApiError:
    return ApiError(402, "insufficient_credits", f"可用额度不足：需要 {needed}，可用 {available}", available=available, needed=needed)
