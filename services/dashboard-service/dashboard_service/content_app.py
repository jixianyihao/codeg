"""The isolated content origin (docs/aresclaw-dashboard/contracts.md §7).

Only the trusted loader pages (GET /view/{id}, legacy GET /render, their
scripts) and GET /content live here. /content accepts view capabilities
exclusively — a W3 or service JWT is rejected, and capabilities never work
against the control API.
"""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from .config import Config
from .errors import ApiError, new_id, require, require_uuid
from .routers import Service

BOOTSTRAP_CSP = (
    "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; connect-src 'self'; frame-src about:; "
    "object-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'"
)

# The loader is never embedded by anything — the control-origin entry
# navigates the browser to it (single layer; docs/aresclaw-dashboard/design.md,
# "List management and viewing isolation").
LOADER_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def _loader_response(web_dir: Path, policy: str, *,
                     dashboard_id: str | None = None,
                     control_origin: str | None = None) -> Response:
    if dashboard_id is None:
        # Legacy /render: no positional information — the page shows a
        # generic back-hint instead of guessing a dashboard.
        return FileResponse(web_dir / "render.html", media_type="text/html",
                            headers={"Content-Security-Policy": policy,
                                     **LOADER_HEADERS})
    page = (web_dir / "render.html").read_text(encoding="utf-8")
    # Inject the trusted coordinates for the back-to-control link; the
    # capability itself never leaves the fragment.
    marker = ('<meta name="x-dashboard-control-origin" content="%s">'
              '<meta name="x-dashboard-id" content="%s">' % (control_origin, dashboard_id))
    injected = page.replace("</head>", marker + "</head>", 1)
    if injected == page:  # defensive: never serve an unannotated page
        injected = marker + page
    return HTMLResponse(injected, headers={
        "Content-Security-Policy": policy, **LOADER_HEADERS})


def create_content_app(service: Service) -> FastAPI:
    config = service.config
    app = FastAPI(title="AresClaw Dashboard Content", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.service = service
    web_dir = Path(__file__).resolve().parent.parent / "web"
    bootstrap_policy = f"{BOOTSTRAP_CSP}; frame-ancestors 'none'"

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError):
        from .errors import new_id
        return Response(
            content='{"code":"%s","message":"%s","trace_id":"%s","retryable":false}'
            % (error.code, error.message.replace('"', "'"), new_id()),
            status_code=error.status, media_type="application/json",
            headers={"Cache-Control": "no-store"})

    @app.get("/view/{dashboard_id}")
    def view_loader(dashboard_id: str):
        """Single-layer trusted loader: reads and clears the #capability,
        fetches /content, renders it in exactly one sandboxed iframe that
        fills the viewport."""
        require_uuid(dashboard_id, "dashboard_id")
        return _loader_response(web_dir, bootstrap_policy,
                                dashboard_id=dashboard_id,
                                control_origin=config.control_origin)

    @app.get("/render")
    def render():
        return _loader_response(web_dir, bootstrap_policy)

    @app.get("/render.js")
    def render_script():
        return FileResponse(web_dir / "render.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store",
                                     "X-Content-Type-Options": "nosniff"})

    @app.get("/content")
    async def content(request: Request):
        header = request.headers.get("authorization") or ""
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        require(bool(token), 401, "invalid_capability", "A view capability is required")

        def run():
            if config.recovery_mode:
                raise ApiError(503, "recovery_isolation",
                               "Service is isolated for recovery")
            version = service.capabilities.resolve(token)
            html = service.store.read_version(version)
            return Response(
                html, media_type="text/plain; charset=utf-8",
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                    "Content-Disposition": "inline",
                })

        return await run_in_threadpool(run)

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    return app
