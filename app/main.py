"""NOVALYN API application."""

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.errors import error_body, register_error_handlers
from app.core.logging import configure_logging, request_id_var

logger = logging.getLogger("app.request")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    # API responses are data: nothing in them may run, load or be framed.
    # File downloads set their own, slightly wider policy so the preview can frame them.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "Cross-Origin-Resource-Policy": "same-site",
}
HSTS = "max-age=63072000; includeSubDomains"
# The interactive API docs (development only) load their own scripts and styles.
DOCS_PATHS = ("/api/docs", "/api/openapi.json")


def check_database_role() -> None:
    """Row-Level Security does not apply to superusers or BYPASSRLS roles.

    Refuse to serve as one in production; warn elsewhere.
    """
    from app.core.rls import role_bypasses_rls

    settings = get_settings()
    with SessionLocal() as db:
        bypasses = role_bypasses_rls(db.connection())
    if not bypasses:
        return
    message = (
        "The database role skips Row-Level Security (superuser or BYPASSRLS). "
        "Connect as an ordinary role that owns the tables; see DEPLOYMENT.md."
    )
    if settings.is_production:
        raise RuntimeError(message)
    logging.getLogger(__name__).warning(message)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    for warning in get_settings().production_warnings:
        logging.getLogger(__name__).warning("Production setting: %s", warning)
    check_database_role()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    docs_enabled = not settings.is_production
    app = FastAPI(
        lifespan=lifespan,
        title="NOVALYN API",
        description="Document intelligence, certificate management and renewal tracking.",
        version="0.1.0",
        docs_url="/api/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if docs_enabled else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Requested-With", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Retry-After"],
        max_age=600,
    )

    # Multipart framing and the other form fields add a little to the file itself.
    max_body_bytes = settings.max_upload_size_bytes + 1024 * 1024

    @app.middleware("http")
    async def request_context(request: Request, call_next) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if 8 <= len(incoming) <= 64 and incoming.isascii() else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            try:
                declared = request.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_body_bytes:
                    # Refuse oversized bodies before reading them.
                    response = JSONResponse(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        content=error_body(
                            "file_too_large", f"Files can be at most {settings.max_upload_size_mb} MB."
                        ),
                    )
                else:
                    response = await call_next(request)
            except Exception:
                logger.exception("Unhandled error on %s %s", request.method, request.url.path)
                response = JSONResponse(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    content=error_body("internal_error", "Something went wrong on our side."),
                )
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            response.headers["X-Request-ID"] = request_id
            if not request.url.path.startswith(DOCS_PATHS):
                for header, value in SECURITY_HEADERS.items():
                    response.headers.setdefault(header, value)
            if settings.is_production:
                response.headers.setdefault("Strict-Transport-Security", HSTS)
            logger.info(
                "%s %s -> %s (%sms)",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                extra={"status_code": response.status_code, "duration_ms": duration_ms},
            )
            return response
        finally:
            request_id_var.reset(token)

    register_error_handlers(app)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    def ready() -> JSONResponse:
        """Readiness: the database answers."""
        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
        except Exception:
            logger.exception("Readiness check failed")
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "unavailable", "database": "error"},
            )
        return JSONResponse(content={"status": "ok", "database": "ok"})

    return app


app = create_app()
