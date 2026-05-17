"""AppError 体系基本属性。"""

from __future__ import annotations

import pytest

from app.core.errors import (
    AppError,
    ConflictError,
    NotFoundError,
    RateLimitedError,
    UpstreamError,
    ValidationError,
)


def test_app_error_default() -> None:
    err = AppError("boom")
    assert err.status_code == 400
    assert err.code == "app_error"
    assert err.to_payload() == {"code": "app_error", "message": "boom"}


def test_subclass_status_code() -> None:
    cases = [
        (NotFoundError("x"), 404, "not_found"),
        (ValidationError("x"), 422, "validation_error"),
        (ConflictError("x"), 409, "conflict"),
        (RateLimitedError("x"), 429, "rate_limited"),
        (UpstreamError("x"), 502, "upstream_error"),
    ]
    for err, status, code in cases:
        assert err.status_code == status
        assert err.code == code


def test_details_in_payload() -> None:
    err = AppError("x", details={"k": "v"})
    assert err.to_payload()["details"] == {"k": "v"}


def test_overrides() -> None:
    err = AppError("x", code="custom", status_code=418)
    assert err.code == "custom"
    assert err.status_code == 418


def test_is_exception() -> None:
    with pytest.raises(AppError):
        raise NotFoundError("missing")
