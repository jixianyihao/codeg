"""App factories. The CLI serves both origins in one worker process."""
import asyncio
import logging
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from .api import API
from .auth import Auth
from .config import Config
from .content import register_content
from .models import ApiError, identifier
from .store import Store

logger = logging.getLogger('dashboard_service')


class Guardrails:
    def __init__(self, app, config):
        self.app, self.config = app, config
        self.slots = asyncio.Semaphore(config.max_uploads)

    async def __call__(self, scope, receive, send):
        if scope['type']!='http':
            return await self.app(scope,receive,send)
        async def safe_send(message):
            if message['type']=='http.response.start':
                headers = list(message.get('headers',[]))
                headers.extend([(b'cache-control',b'no-store'),(b'x-content-type-options',b'nosniff'),(b'referrer-policy',b'no-referrer'),(b'permissions-policy',b'camera=(), microphone=(), geolocation=()')])
                message['headers'] = headers
            await send(message)
        if scope['method'] not in ('POST','PUT','PATCH'):
            return await self.app(scope,receive,safe_send)
        headers = dict(scope.get('headers',[]))
        limit = self.config.max_upload_bytes+65536 if b'multipart/form-data' in headers.get(b'content-type',b'') else 65536
        try:
            declared = int(headers.get(b'content-length',b'0'))
        except ValueError:
            declared = limit+1
        if declared>limit:
            return await JSONResponse(dict(code='upload_too_large',message='Request body exceeds limit',trace_id=identifier()),status_code=413)(scope,receive,safe_send)
        try:
            await asyncio.wait_for(self.slots.acquire(),timeout=0.05)
        except TimeoutError:
            return await JSONResponse(dict(code='upload_busy',message='Upload concurrency limit reached',trace_id=identifier()),status_code=429)(scope,receive,safe_send)
        total = 0
        async def bounded_receive():
            nonlocal total
            message = await receive()
            total += len(message.get('body',b''))
            if total>limit:
                raise ApiError(413,'upload_too_large','Request body exceeds limit')
            return message
        try:
            await self.app(scope,bounded_receive,safe_send)
        finally:
            self.slots.release()


def configure_app(app, config):
    app.add_middleware(Guardrails,config=config)

    @app.exception_handler(ApiError)
    async def api_error(request: Request,error: ApiError):
        return JSONResponse(dict(code=error.code,message=error.message,trace_id=identifier()),status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request,error: RequestValidationError):
        return JSONResponse(dict(code='invalid_input',message='Invalid request fields',trace_id=identifier()),status_code=422)

    @app.exception_handler(SQLAlchemyError)
    @app.exception_handler(OSError)
    async def storage_error(request: Request,error: Exception):
        trace = identifier()
        logger.error('storage_unavailable trace_id=%s error_type=%s',trace,type(error).__name__)
        return JSONResponse(dict(code='storage_unavailable',message='Storage is unavailable; retry using the original request key',trace_id=trace),status_code=503)


def create_app(config=None, *, store=None, test_configuration=False):
    config = (config or (Config.testing() if test_configuration else Config.from_env())).validate()
    store = store or Store(config)
    auth = Auth(config,store)
    api = API(config,store,auth)
    app = FastAPI(title='AresClaw Dashboard',docs_url=None,redoc_url=None,openapi_url=None)
    content_app = FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    configure_app(app,config)
    configure_app(content_app,config)
    app.state.store, app.state.auth, app.state.config = store,auth,config
    app.state.content_app = content_app
    app.include_router(api.router)
    web_dir = Path(__file__).resolve().parent.parent/'web'
    register_content(app,content_app,api,web_dir)
    control_policy = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-src " + config.content_origin + "; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"

    @app.get('/dashboards/{ident}')
    def shell(ident: str):
        return FileResponse(web_dir/'index.html',media_type='text/html',headers={'Content-Security-Policy':control_policy})

    @app.get('/app.js')
    def javascript():
        return FileResponse(web_dir/'app.js',media_type='text/javascript')

    @app.get('/auth-provider.js')
    def auth_provider():
        return FileResponse(web_dir/'auth-provider.js',media_type='text/javascript')

    @app.get('/styles.css')
    def stylesheet():
        return FileResponse(web_dir/'styles.css',media_type='text/css')

    @app.get('/health/live')
    def live():
        return dict(status='ok')

    @app.get('/health/ready')
    def ready():
        from sqlalchemy import select
        from .store import locks
        from .models import require
        require(not config.recovery_mode,503,'recovery_isolation','Service is isolated for recovery')
        with store.engine.connect() as connection:
            require(connection.execute(select(locks.c.schema_version).where(locks.c.id==1)).scalar_one()==1,503,'schema_mismatch','Database schema mismatch')
        require(store.root.is_dir(),503,'storage_unavailable','Content storage is unavailable')
        return dict(status='ok',human_auth='configured' if config.w3_verify_url else 'disabled')
    return app
