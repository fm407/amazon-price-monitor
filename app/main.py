"""Application factory and lifecycle."""

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app import __version__, alerts, api, demo, products, security, web
from app.amazon import InvalidProductInput
from app.config import Settings
from app.context import AppContext, build_context
from app.db import run_migrations
from app.logging_config import configure_logging, job_id_var
from app.scheduler import Scheduler
from app.schemas import Envelope, ErrorBody

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    429: "rate_limited",
}
# Domain errors and the HTTP status each one maps to.
DOMAIN_ERRORS: dict[type[Exception], tuple[int, str]] = {
    InvalidProductInput: (422, "invalid_product_input"),
    products.InvalidProductUpdate: (422, "invalid_product_update"),
    alerts.InvalidRule: (422, "invalid_alert_rule"),
    products.DuplicateProduct: (409, "duplicate_product"),
    products.ProductNotFound: (404, "product_not_found"),
}
DESCRIPTION = (
    "Self-hosted Amazon price monitoring. Authenticate with the dashboard session cookie (send the "
    "X-CSRF-Token header on state-changing requests) or with `Authorization: Bearer <API_TOKEN>`."
)


def error_response(status: int, code: str, message: str, details: list[dict] | None = None) -> JSONResponse:
    body = Envelope[None](success=False, error=ErrorBody(code=code, message=message, details=details))
    return JSONResponse(body.model_dump(mode="json"), status_code=status)


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(web.LoginRequired)
    async def login_required(request: Request, exc: web.LoginRequired) -> Response:
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if _is_api(request):
            return error_response(exc.status_code, ERROR_CODES.get(exc.status_code, "error"), str(exc.detail))
        return web.render(request, "error.html", status_code=exc.status_code, message=str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        # Report where and why, but never echo the submitted values back.
        details = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]
        if _is_api(request):
            return error_response(422, "validation_error", "The request is not valid.", details)
        return web.render(request, "error.html", status_code=422, message="The submitted form is not valid.")

    async def domain_error(request: Request, exc: Exception) -> Response:
        status, code = DOMAIN_ERRORS[type(exc)]
        if _is_api(request):
            return error_response(status, code, str(exc))
        return web.render(request, "error.html", status_code=status, message=str(exc))

    for error_type in DOMAIN_ERRORS:
        app.add_exception_handler(error_type, domain_error)

    @app.exception_handler(OperationalError)
    async def database_error(request: Request, exc: OperationalError) -> Response:
        log.error("database error", extra={"path": request.url.path, "error": type(exc.orig).__name__})
        message = "The database is busy or unavailable. Try again in a moment."
        if _is_api(request):
            return error_response(503, "database_unavailable", message)
        return web.render(request, "error.html", status_code=503, message=message)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> Response:
        log.exception("unhandled error", extra={"path": request.url.path})
        message = "Internal error. The details are in the application log."
        # This handler runs outside the middleware above, so it adds the security headers itself.
        if _is_api(request):
            return finish_response(request, error_response(500, "internal_error", message))
        return finish_response(request, web.render(request, "error.html", status_code=500, message=message))


def finish_response(request: Request, response: Response, request_id: str = "") -> Response:
    for header, value in security.SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    if request_id:
        response.headers["X-Request-ID"] = request_id
    if not request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def create_app(settings: Settings | None = None, ctx: AppContext | None = None) -> FastAPI:
    if ctx is None:
        settings = settings or Settings()
        configure_logging(settings.log_level, settings.secret_values())
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        run_migrations(settings.database_url)
        ctx = build_context(settings)
    settings = ctx.settings
    scheduler = Scheduler(ctx)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "application startup",
            extra={"version": __version__, "provider": settings.provider, "demo_mode": settings.demo_mode},
        )
        security.ensure_setup_token(ctx)
        demo.sync(ctx)
        if settings.scheduler_enabled:
            scheduler.start()
            app.state.scheduler = scheduler
        yield
        # SIGTERM lands here: stop scheduling, let running checks finish, release resources.
        if settings.scheduler_enabled:
            scheduler.stop()
        ctx.close()
        log.info("application shutdown complete")

    app = FastAPI(
        title="Amazon Price Monitor",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url=None,  # the interactive docs need third-party CDN assets; the schema is served instead
        redoc_url=None,
        openapi_url=None,
    )
    app.state.ctx = ctx
    app.state.throttle = security.LoginThrottle()

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = f"req-{uuid.uuid4().hex[:12]}"
        token = job_id_var.set(request_id)
        try:
            rejected = security.body_too_large(request)
            if rejected:
                response = error_response(
                    rejected, "payload_rejected", "The request body is too large or has no length."
                )
            else:
                response = await call_next(request)
        finally:
            job_id_var.reset(token)
        return finish_response(request, response, request_id)

    app.add_middleware(
        SessionMiddleware,
        secret_key=security.session_secret(ctx),
        session_cookie="apm_session",
        max_age=settings.session_max_age_hours * 3600,
        same_site="strict",
        https_only=settings.session_cookie_secure,
    )
    register_error_handlers(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(api.public)
    app.include_router(api.router)
    app.include_router(web.public)
    app.include_router(web.router)

    return app
