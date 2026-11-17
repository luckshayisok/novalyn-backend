"""Application errors and the handlers that turn them into one response shape.

Every error response is:

    {"error": {"code": "...", "message": "...", "details": ..., "request_id": "..."}}
"""

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import request_id_var

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "bad_request"
    message: str = "The request could not be processed."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message
        if code:
            self.code = code
        self.details = details
        self.headers = headers


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "not_authenticated"
    message = "Authentication is required."


class PermissionDeniedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"
    message = "You do not have permission to do this."


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "The requested resource was not found."


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "The request conflicts with the current state."


class ValidationFailedError(AppError):
    status_code = 422
    code = "validation_error"
    message = "The submitted data is not valid."


class RateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many requests. Try again later."


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "request_id": request_id_var.get(),
        }
    }


def _readable(error: dict[str, Any]) -> str:
    """Turn a Pydantic validation message into one a person filling in a form can act on."""
    message = str(error.get("msg", "This value is not valid."))
    if error.get("type") == "missing":
        return "This field is required."
    if message.startswith("value is not a valid email address"):
        return "Enter a valid email address."
    if message.startswith("Value error, "):
        message = message.removeprefix("Value error, ")
    return message[:1].upper() + message[1:]


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.details),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "field": ".".join(str(part) for part in error["loc"] if part != "body"),
                "message": _readable(error),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_body("validation_error", "The submitted data is not valid.", details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {
            401: "not_authenticated",
            403: "permission_denied",
            404: "not_found",
            405: "method_not_allowed",
        }
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(codes.get(exc.status_code, "http_error"), str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_body("internal_error", "Something went wrong on our side."),
        )
