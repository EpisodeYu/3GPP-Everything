"""统一异常体系。

- `AppError`：业务错误基类，FastAPI exception_handler 映射为 4xx JSON
- 子类：分类标记 HTTP status + code 字符串
- 5xx 走未捕获 -> 默认 500 + 统一 logger

设计目标：业务代码只 raise 子类，不直接构造 HTTPException；handler 集中映射。
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """业务错误基类。

    HTTP 映射在 FastAPI exception_handler 实现；这里只携带 code / message / status / details。
    """

    status_code: int = 400
    code: str = "app_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return body


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"


class UpstreamError(AppError):
    """外部依赖（LiteLLM / Qdrant / Voyage / Tavily / Redis / PG）失败。"""

    status_code = 502
    code = "upstream_error"


class RetrievalError(UpstreamError):
    code = "retrieval_failed"


class LLMError(UpstreamError):
    code = "llm_failed"
