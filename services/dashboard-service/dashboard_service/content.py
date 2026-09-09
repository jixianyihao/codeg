"""Isolated fixed content origin with short-lived single-version capabilities."""
import hashlib
import secrets
import threading
import time
from datetime import datetime, timezone
from fastapi import Body, Request
from fastapi.responses import FileResponse, Response
from .models import require, timestamp


class Capabilities:
    def __init__(self, api):
        self.api, self.lock = api, threading.Lock()
        self.values, self.by_operation = {}, {}

    def issue(self, request, ident, payload):
        actor = self.api.actor(request)
        require(actor['principal_type']=='human',403,'human_required','Rendering requires a human identity')
        def apply(connection,operation_id):
            dashboard = self.api.store.dashboard(ident,connection)
            require(dashboard['status']=='published',404,'not_found','Dashboard is not visible')
            version_id = payload.get('version_id') or dashboard['current_version_id']
            version = self.api.store.get('version',version_id,connection)
            require(version and version['dashboard_id']==ident,404,'not_found','Version is not visible')
            return dict(operation_id=operation_id,dashboard_id=ident,version_id=version_id,expires_at=timestamp(datetime.fromtimestamp(min(time.time()+60,actor['expires']),timezone.utc)))
        result = self.api.mutation(request,actor,payload,apply,'read',ident)
        expiry = datetime.fromisoformat(result['expires_at'].replace('Z','+00:00')).timestamp()
        require(expiry>time.time(),409,'capability_expired','Request a new view capability with a new idempotency key')
        with self.lock:
            self.values = {key:value for key,value in self.values.items() if value['expires']>time.time()}
            self.by_operation = {key:token for key,token in self.by_operation.items() if hashlib.sha256(token.encode()).hexdigest() in self.values}
            require(len(self.values)<10000,429,'capability_busy','Too many active views; retry later')
            token = self.by_operation.get(result['operation_id']) or secrets.token_urlsafe(32)
            self.by_operation[result['operation_id']] = token
            self.values[hashlib.sha256(token.encode()).hexdigest()] = dict(expires=expiry,actor=actor,credential=self.api.auth.bearer(request.headers.get('authorization')),dashboard_id=ident,version_id=result['version_id'])
        return dict(render_url=self.api.config.content_origin+'/render#'+token,expires_at=result['expires_at'])

    def read(self, token):
        with self.lock:
            capability = self.values.get(hashlib.sha256(token.encode()).hexdigest())
        require(capability and capability['expires']>time.time(),401,'invalid_capability','View capability is invalid or expired')
        actor = self.api.auth.authenticate(capability['credential'])
        require(actor['principal_type']=='human' and actor['principal_id']==capability['actor']['principal_id'],401,'invalid_capability','View identity has changed')
        dashboard = self.api.store.dashboard(capability['dashboard_id'])
        self.api.auth.authorize(dashboard,actor)
        require(dashboard['status']=='published',404,'not_found','Dashboard is not visible')
        version = self.api.store.get('version',capability['version_id'])
        require(version and version['dashboard_id']==dashboard['id'],404,'not_found','Version is not visible')
        content = self.api.store.read_content(version)
        self.api.auth.authorize(self.api.store.dashboard(dashboard['id']),actor)
        require(capability['expires']>time.time(),401,'invalid_capability','View capability expired')
        return content


def register_content(control_app, content_app, api, web_dir):
    capabilities = Capabilities(api)
    control_app.state.capabilities = content_app.state.capabilities = capabilities

    @control_app.post('/api/v1/dashboards/{ident}/view-capabilities')
    def issue(ident: str,request: Request,payload: dict=Body(default={})):
        return capabilities.issue(request,ident,payload)

    @content_app.get('/content')
    def content(request: Request):
        require(not api.config.recovery_mode,503,'recovery_isolation','Service is isolated for recovery')
        value = capabilities.read(api.auth.bearer(request.headers.get('authorization')))
        return Response(value,media_type='text/plain',headers={'Content-Security-Policy':"default-src 'none'; sandbox",'Content-Disposition':'attachment; filename="dashboard.txt"'})

    bootstrap_policy = "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'self'; frame-src about:; base-uri 'none'; form-action 'none'; frame-ancestors " + api.config.control_origin

    @content_app.get('/render')
    def render():
        return FileResponse(web_dir/'render.html',media_type='text/html',headers={'Content-Security-Policy':bootstrap_policy})

    @content_app.get('/render.js')
    def script():
        return FileResponse(web_dir/'render.js',media_type='text/javascript')
