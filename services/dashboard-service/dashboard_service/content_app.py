"""The isolated content origin (contracts.md §6).

Only GET /render, /render.js, GET /content and minimal health routes live
here. /content accepts view capabilities exclusively — a W3 or service JWT
is rejected, and capabilities never work against the control API.
"""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool

from .config import Config
from .errors import ApiError
from .routers import Service

BOOTSTRAP_CSP = (
    "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; connect-src 'self'; frame-src about:; "
    "object-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'"
)


def create_content_app(service: Service) -> FastAPI:
    config = service.config
    app = FastAPI(title="AresClaw Dashboard Content", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.service = service
    web_dir = Path(__file__).resolve().parent.parent / "web"
    bootstrap_policy = f"{BOOTSTRAP_CSP}; frame-ancestors {config.control_origin}"

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError):
        from .errors import new_id
        return Response(
            content='{"code":"%s","message":"%s","trace_id":"%s","retryable":false}'
            % (error.code, error.message.replace('"', "'"), new_id()),
            status_code=error.status, media_type="application/json",
            headers={"Cache-Control": "no-store"})

    @app.get("/render")
    def render():
        return FileResponse(web_dir / "render.html", media_type="text/html",
                            headers={"Content-Security-Policy": bootstrap_policy,
                                     "Cache-Control": "no-store",
                                     "Referrer-Policy": "no-referrer"})

    @app.get("/render.js")
    def render_script():
        return FileResponse(web_dir / "render.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store",
                                     "X-Content-Type-Options": "nosniff"})

    @app.get("/content")
    def content(request: Request):
        header = request.headers.get("authorization") or ""
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        require(bool(token), 401, "invalid_capability", "A view capability is required")

        def run():
            if config.recovery_mode:
                raise ApiError(503, "recovery_isolation",
                               "Service is isolated for recovery")
            version = service.capabilities.resolve(token)
            html = service.store.read_version(
                version["storage_key"], version["sha256"], version["byte_size"])
            return Response(
                html, media_type="text/plain; charset=utf-8",
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                    "Content-Disposition": "inline",
                })

        return run_in_threadpool(run)

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    return app
