"""服务端统一错误类型，HTTP 层按 status/code 映射。"""

from __future__ import annotations


class AppError(Exception):
    """业务错误基类。"""

    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status

    def payload(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


class Unauthorized(AppError):
    status = 401
    code = "unauthorized"


class Forbidden(AppError):
    status = 403
    code = "forbidden"


class NotFound(AppError):
    status = 404
    code = "not_found"


class Conflict(AppError):
    status = 409
    code = "conflict"


class Validation(AppError):
    status = 422
    code = "validation"
