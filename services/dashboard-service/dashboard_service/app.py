"""Control-origin application assembly.

Dev/test entry points:
  python -m dashboard_service serve-control   (uvicorn, port from env or 8080)
  python -m dashboard_service serve-content   (uvicorn, port from env or 8081)
Production runs the two apps as separate processes (design.md §9).
"""
import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy.exc import SQLAlchemyError

from .config import Config
from .content_app import create_content_app
from .database import Database, create_db_engine
from .errors import ApiError, new_id
from .routers import build_service
from .routers import access as access_router
from .routers import dashboards as dashboards_router
from .routers import groups_router, meta as meta_router, operations_router, view_router
from .authn import Authenticator, HttpW3Verifier

logger = logging.getLogger("dashboard_service")

CONTROL_PAGE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-src {content}; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)


class RequestGuards:
    """Trace ids, no-store on dynamic responses, and hard body caps enforced
    on the bytes actually received — a declared Content-Length is only a
    fast precheck, never the limit itself (the streaming stage re-validates
    the true multipart byte count in storage.stage_stream)."""

    def __init__(self, app, config: Config):
        self.app = app
        self.config = config
        self.upload_slots = asyncio.Semaphore(config.max_concurrent_uploads)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        is_multipart = b"multipart/form-data" in headers.get(b"content-type", b"")
        limit = self.config.max_upload_bytes + 262144 if is_multipart else 131072
        received = 0
        state = scope.setdefault("state", {})
        state["trace_id"] = new_id()

        async def safe_send(message):
            if message["type"] == "http.response.start":
                header_list = list(message.get("headers", []))
                header_list.extend([
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                ])
                message["headers"] = header_list
            await send(message)

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                # Count the ACTUAL bytes flowing through, never trusting a
                # declared Content-Length: chunked uploads and lying headers
                # are cut off before any parser caches them (R6).
                received += len(message.get("body", b"") or b"")
                if received > limit:
                    response = JSONResponse(
                        {"code": "upload_too_large", "message": "Request body exceeds limit",
                         "trace_id": state["trace_id"], "retryable": False},
                        status_code=413)
                    await response(scope, receive, safe_send)
                    raise _ClientAbort()
            return message

        slot_acquired = False
        if is_multipart and scope["method"] == "POST":
            try:
                await asyncio.wait_for(self.upload_slots.acquire(), timeout=30)
                slot_acquired = True
            except TimeoutError:
                response = JSONResponse(
                    {"code": "upload_busy", "message": "Upload concurrency limit reached",
                     "trace_id": state["trace_id"], "retryable": True},
                    status_code=429, headers={"Retry-After": "3"})
                await response(scope, receive, safe_send)
                return
        try:
            await self.app(scope, bounded_receive, safe_send)
        except _ClientAbort:
            return
        finally:
            if slot_acquired:
                self.upload_slots.release()


class _ClientAbort(Exception):
    pass


def _error_body(code: str, message: str, trace_id: str, retryable: bool = False,
                operation_id: str | None = None) -> dict:
    body = {"code": code, "message": message, "trace_id": trace_id, "retryable": retryable}
    if operation_id:
        body["operation_id"] = operation_id
    return body


def create_control_app(config: Config | None = None, *, database: Database | None = None,
                       verifier=None) -> FastAPI:
    config = config or Config.from_env()
    database = database or Database(create_db_engine(config.database_url))
    verifier = verifier or HttpW3Verifier.from_config(config)
    authenticator = Authenticator(config, database, verifier)
    service = build_service(config, database, authenticator)

    app = FastAPI(title="AresClaw Dashboard", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.service = service
    app.add_middleware(RequestGuards, config=config)

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError):
        trace_id = getattr(request.state, "trace_id", None) or new_id()
        return JSONResponse(
            _error_body(error.code, error.message, trace_id, error.retryable),
            status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return JSONResponse(
            _error_body("invalid_input", "Invalid request fields",
                        getattr(request.state, "trace_id", None) or new_id()),
            status_code=422)

    @app.exception_handler(SQLAlchemyError)
    @app.exception_handler(OSError)
    async def storage_error(request: Request, error: Exception):
        trace_id = getattr(request.state, "trace_id", None) or new_id()
        logger.error("storage_unavailable trace_id=%s error_type=%s", trace_id,
                     type(error).__name__)
        return JSONResponse(
            _error_body("database_unavailable",
                        "A dependency is unavailable; query any operation with its key",
                        trace_id, retryable=True),
            status_code=503)

    from .routers.dashboards import PendingOperation

    @app.exception_handler(PendingOperation)
    async def pending(request: Request, pending: PendingOperation):
        trace_id = getattr(request.state, "trace_id", None) or new_id()
        return JSONResponse(
            pending.outcome.wrapper | {"trace_id": trace_id},
            status_code=202, headers={"Retry-After": str(pending.outcome.retry_after or 2)})

    for module in (meta_router, dashboards_router, access_router, groups_router,
                   operations_router, view_router):
        app.include_router(module.router)

    web_dir = Path(__file__).resolve().parent.parent / "web"
    page_policy = CONTROL_PAGE_CSP.format(content=config.content_origin)

    @app.get("/dashboards/{dashboard_id}", include_in_schema=False)
    def dashboard_page(dashboard_id: str):
        return FileResponse(web_dir / "index.html", media_type="text/html",
                            headers={"Content-Security-Policy": page_policy})

    @app.get("/dashboards/{dashboard_id}/view", include_in_schema=False)
    def dashboard_full_view(dashboard_id: str):
        """Stable bookmarkable route: full-viewport render only. The visitor
        authenticates with their own identity; a fresh short-lived capability
        is minted per load, so the URL stays valid while ACL changes apply
        immediately."""
        return FileResponse(web_dir / "view.html", media_type="text/html",
                            headers={"Content-Security-Policy": page_policy})

    @app.get("/view.js", include_in_schema=False)
    def view_script():
        return FileResponse(web_dir / "view.js", media_type="text/javascript",
                            headers={"X-Content-Type-Options": "nosniff",
                                     "Cache-Control": "no-store"})

    for name, media in (("app.js", "text/javascript"), ("styles.css", "text/css")):
        def static_file(name=name, media=media):
            # Admin-page assets are small and change with the service;
            # no-store prevents webviews from running stale login/view
            # scripts against a freshly restarted backend.
            return FileResponse(web_dir / name, media_type=media,
                                headers={"X-Content-Type-Options": "nosniff",
                                         "Cache-Control": "no-store"})
        app.get(f"/{name}", include_in_schema=False)(static_file)

    @app.get("/auth-provider.js", include_in_schema=False)
    def auth_provider():
        # DEV-ONLY override (loopback-gated in Config): serves the trial
        # login adapter instead of the fail-closed production stub.
        dev_path = web_dir / "auth-provider.dev.js"
        source = (dev_path if config.dev_login and dev_path.exists()
                  else web_dir / "auth-provider.js")
        return FileResponse(source, media_type="text/javascript",
                            headers={"Cache-Control": "no-store",
                                     "X-Content-Type-Options": "nosniff"})

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(web_dir / "index.html", media_type="text/html",
                            headers={"Content-Security-Policy": page_policy})

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        from sqlalchemy import text
        checks = {"database": "ok", "human_auth":
                  "configured" if config.w3_verify_url else "disabled"}
        try:
            database.verify_schema()
        except RuntimeError as error:
            checks["database"] = f"degraded: {error}"
        checks.update(service.store.probe())
        healthy = checks["database"] == "ok" and checks["s3"] == "ok"
        return JSONResponse({"status": "ok" if healthy else "degraded", "checks": checks,
                             "recovery_mode": config.recovery_mode},
                            status_code=200 if healthy else 503)

    @app.get("/api/v1/health/deep", include_in_schema=False)
    def deep():
        with database.read_only() as connection:
            connection.execute(text("SELECT 1"))
        return Response("ok", media_type="text/plain")

    return app


__all__ = ["create_control_app", "create_content_app", "build_service"]
