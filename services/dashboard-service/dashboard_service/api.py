"""Versioned control API. Generated HTML is never served as HTML here."""
import hashlib
import json
import threading
from contextlib import contextmanager

from fastapi import APIRouter, Body, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from .models import actions, identifier, require, revision, text_field, timestamp


class API:
    def __init__(self, config, store, auth):
        self.config, self.store, self.auth = config,store,auth
        self.upload_lock = threading.Lock()
        self.upload_counts = {}
        self.router = APIRouter(prefix='/api/v1')
        self.register()

    def actor(self, request):
        require(not self.config.recovery_mode,503,'recovery_isolation','Service is isolated for recovery')
        return self.auth.authenticate(self.auth.bearer(request.headers.get('authorization')))

    def read(self, request, ident, action='read'):
        actor = self.actor(request)
        dashboard = self.store.dashboard(ident)
        self.auth.authorize(dashboard,actor,action)
        return actor,dashboard

    def view(self, dashboard, actor, connection=None):
        access = self.auth.effective_access(dashboard,actor,connection)
        return {key:value for key,value in dashboard.items() if key != 'grants'} | dict(role=access['role'],expires_at=access['expires_at'])

    def mutation(self, request, actor, payload, action, scope='manage', ident=None):
        def check(connection):
            self.auth.recheck(actor,scope,connection)
            if ident:
                self.auth.authorize(self.store.dashboard(ident,connection),actor,scope,connection)
        return self.store.mutate(actor,request.headers.get('idempotency-key'),request.method,request.url.path,payload,action,check)

    def change_dashboard(self, request, ident, payload, change, scope='manage'):
        actor = self.actor(request)
        def apply(connection, operation_id):
            dashboard = self.store.dashboard(ident,connection)
            revision(dashboard,payload.get('expected_revision'))
            change(dashboard,connection)
            dashboard['revision'] += 1
            dashboard['updated_at'] = timestamp()
            self.store.put('dashboard',dashboard,connection)
            return self.view(dashboard,actor,connection) | {'operation_id':operation_id}
        return self.mutation(request,actor,payload,apply,scope,ident)

    @staticmethod
    def page(items, cursor=None, limit=50):
        require(1 <= limit <= 100,message='limit must be between 1 and 100')
        ordered = sorted(items,key=lambda value:value['id'])
        if cursor:
            ordered = [value for value in ordered if value['id'] > cursor]
        page = ordered[:limit]
        return dict(items=page,next_cursor=page[-1]['id'] if len(ordered)>limit else None)

    @contextmanager
    def upload_slot(self, principal):
        with self.upload_lock:
            require(sum(self.upload_counts.values()) < self.config.max_uploads and self.upload_counts.get(principal,0) < self.config.max_principal_uploads,
                    429,'upload_busy','Upload concurrency limit reached')
            self.upload_counts[principal] = self.upload_counts.get(principal,0)+1
        try:
            yield
        finally:
            with self.upload_lock:
                self.upload_counts[principal] -= 1
                if not self.upload_counts[principal]:
                    del self.upload_counts[principal]

    async def publish(self, request, ident=None):
        actor = await run_in_threadpool(self.actor,request)
        require('write' in actor['scopes'],403,'insufficient_scope','Credential lacks write scope')
        with self.upload_slot(actor['principal_id']):
            async with request.form(max_files=1,max_fields=1,max_part_size=65536) as form:
                parts = list(form.multi_items())
                require(len(parts)==2 and parts[0][0]=='metadata' and parts[1][0]=='html' and isinstance(parts[0][1],str) and isinstance(parts[1][1],UploadFile),
                        message='Upload requires metadata JSON first, then one html file')
                try:
                    metadata = json.loads(parts[0][1])
                except ValueError:
                    require(False,message='metadata must be JSON')
                require(isinstance(metadata,dict),message='metadata must be an object')
                require(set(metadata) <= {'title','description','content_sha256','byte_size','expected_revision'},message='Unknown metadata fields')
                title = text_field(metadata,'title',200,True)
                description = text_field(metadata,'description',2000)
                content = await parts[1][1].read(self.config.max_upload_bytes+1)
                require(len(content)<=self.config.max_upload_bytes,413,'upload_too_large','HTML exceeds the 10 MiB limit')
                require(content and type(metadata.get('byte_size')) is int and len(content)==metadata['byte_size'],message='HTML byte_size does not match')
                try:
                    content.decode('utf-8')
                except UnicodeDecodeError:
                    require(False,message='HTML must be UTF-8')
                digest = hashlib.sha256(content).hexdigest()
                require(metadata.get('content_sha256')==digest,message='HTML SHA256 does not match')
            # Revalidate W3 revocation and identity after the upload, immediately before commit.
            current = await run_in_threadpool(self.actor,request)
            require(current['principal_id']==actor['principal_id'],401,'identity_changed','Credential identity changed')
            actor = current
            created_file = []
            def apply(connection, operation_id):
                date = timestamp()
                if ident:
                    dashboard = self.store.dashboard(ident,connection)
                    revision(dashboard,metadata.get('expected_revision'))
                    require(dashboard['status']=='published',409,'dashboard_archived','Restore the dashboard before publishing')
                else:
                    dashboard_id = identifier()
                    dashboard = dict(id=dashboard_id,title=title,description=description,owner_principal_id=actor['principal_id'],
                        owner_name=actor['display_name'],owner_type=actor['principal_type'],current_version_id='',revision=0,status='published',
                        created_at=date,updated_at=date,published_at=date,view_url=self.config.control_origin+'/dashboards/'+dashboard_id,grants=[])
                versions = self.store.all('version',connection)
                dashboards = self.store.all('dashboard',connection)
                owned = {d['id'] for d in dashboards if d['owner_principal_id']==dashboard['owner_principal_id']}
                require(ident or len(owned)<self.config.max_owner_dashboards,429,'quota_exceeded','Owner dashboard quota exceeded')
                history = [v for v in versions if v['dashboard_id']==dashboard['id']]
                require(len(history)<self.config.max_versions,429,'quota_exceeded','Dashboard version quota exceeded')
                require(sum(v['byte_size'] for v in versions if v['dashboard_id'] in owned)+len(content)<=self.config.max_owner_bytes,
                        429,'quota_exceeded','Owner content quota exceeded')
                actual_bytes = sum(path.stat().st_size for path in self.store.root.glob('*.html'))
                require(actual_bytes+len(content)<=self.config.max_total_bytes,507,'storage_quota','Content storage quota exceeded')
                version_id = identifier()
                created_file.append(self.store.write_content(version_id,content))
                self.store.put('version',dict(id=version_id,dashboard_id=dashboard['id'],number=len(history)+1,sha256=digest,byte_size=len(content),created_at=date,created_by=actor['principal_id']),connection)
                dashboard.update(title=title,description=description,current_version_id=version_id,revision=dashboard['revision']+1,updated_at=date,published_at=date)
                self.store.put('dashboard',dashboard,connection)
                return dict(operation_id=operation_id,state='succeeded',dashboard_id=dashboard['id'],version_id=version_id,revision=dashboard['revision'],view_url=dashboard['view_url'],sha256=digest)
            try:
                return await run_in_threadpool(self.mutation,request,actor,metadata,apply,'write',ident)
            except Exception:
                # An uncertain DB commit must not delete a potentially referenced version.
                # Startup verification removes only unreferenced files older than 24 hours.
                raise

    def register(self):
        router = self.router

        @router.get('/me')
        def me(request: Request):
            actor = self.actor(request)
            return {key:actor[key] for key in ('principal_id','principal_type','display_name','scopes','is_admin')}

        @router.get('/capabilities')
        def capabilities(request: Request):
            self.actor(request)
            return dict(api_major=1,minimum_client_version='0.1.0',features=['publish','versions','rollback','archive','grants','public_access','access_changes','groups','operations','service_accounts','view_capabilities'],
                max_upload_bytes=self.config.max_upload_bytes,auth_methods=['service_jwt']+(['w3'] if self.config.w3_verify_url else []),content_origin=self.config.content_origin)

        @router.get('/dashboards')
        def dashboards(request: Request, scope: str='all', q: str='', cursor: str | None=None, status: str='published',limit: int=50):
            actor = self.actor(request)
            self.auth.recheck(actor,'read',None)
            require(scope in ('mine','shared','all') and status in ('published','archived'))
            visible = []
            for dashboard in self.store.all('dashboard'):
                access = self.auth.effective_access(dashboard,actor)
                mine = dashboard['owner_principal_id']==actor['principal_id']
                if access['role'] and dashboard['status']==status and (scope=='all' or (scope=='mine' and mine) or (scope=='shared' and not mine)) and q.casefold() in (dashboard['title']+' '+dashboard['description']).casefold():
                    visible.append(self.view(dashboard,actor))
            return self.page(visible,cursor,limit)

        @router.post('/dashboards')
        async def create(request: Request):
            return await self.publish(request)

        @router.post('/dashboards/{ident}/versions')
        async def upload_version(ident: str, request: Request):
            return await self.publish(request,ident)

        @router.get('/dashboards/{ident}')
        def detail(ident: str, request: Request):
            actor,dashboard = self.read(request,ident)
            return self.view(dashboard,actor)

        @router.patch('/dashboards/{ident}')
        def rename(ident: str, request: Request, payload: dict=Body(...)):
            require(set(payload)<={'expected_revision','title','description'})
            def change(dashboard, connection):
                if 'title' in payload:
                    dashboard['title'] = text_field(payload,'title',200,True)
                if 'description' in payload:
                    dashboard['description'] = text_field(payload,'description',2000)
            return self.change_dashboard(request,ident,payload,change,'write')

        @router.get('/dashboards/{ident}/versions')
        def versions(ident: str, request: Request,cursor: str | None=None,limit: int=50):
            self.read(request,ident)
            return self.page([{k:v for k,v in version.items() if k!='dashboard_id'} for version in self.store.all('version') if version['dashboard_id']==ident],cursor,limit)

        @router.get('/dashboards/{ident}/versions/{version_id}/source')
        def source(ident: str, version_id: str, request: Request):
            actor,dashboard = self.read(request,ident)
            require(dashboard['status']=='published',409,'dashboard_archived','Dashboard is archived')
            version = self.store.get('version',version_id)
            require(version and version['dashboard_id']==ident,404,'not_found','Version is not visible')
            content = self.store.read_content(version)
            self.auth.authorize(self.store.dashboard(ident),actor)
            return Response(content,media_type='text/plain',headers={'Content-Disposition':f'attachment; filename="{version_id}.txt"','Content-Security-Policy':"default-src 'none'; sandbox"})

        @router.post('/dashboards/{ident}/rollback')
        def rollback(ident: str,request: Request,payload: dict=Body(...)):
            def change(dashboard,connection):
                require(dashboard['status']=='published',409,'dashboard_archived','Restore the dashboard first')
                version = self.store.get('version',payload.get('version_id',''),connection)
                require(version and version['dashboard_id']==ident,404,'not_found','Version is not visible')
                dashboard['current_version_id'] = version['id']
                dashboard['published_at'] = version['created_at']
            return self.change_dashboard(request,ident,payload,change,'write')

        @router.post('/dashboards/{ident}/archive')
        def archive_dashboard(ident: str,request: Request,payload: dict=Body(...)):
            return self.change_dashboard(request,ident,payload,lambda d,c:d.update(status='archived'))

        @router.post('/dashboards/{ident}/restore')
        def restore_dashboard(ident: str,request: Request,payload: dict=Body(...)):
            return self.change_dashboard(request,ident,payload,lambda d,c:d.update(status='published'))

        @router.get('/operations')
        def operation_by_key(request: Request,request_id: str):
            actor = self.actor(request)
            operation = self.store.operation(actor['principal_id'],request_id=request_id)
            require(operation is not None,404,'operation_unknown','No committed operation found; retry the original request with its original key')
            return operation['body']

        @router.get('/operations/{operation_id}')
        def operation_by_id(operation_id: str,request: Request):
            actor = self.actor(request)
            operation = self.store.operation(actor['principal_id'],operation_id=operation_id)
            require(operation is not None,404,'not_found','Operation is not visible')
            return operation['body']

        @router.get('/principals')
        def principals(request: Request,type: str | None=None,q: str='',cursor: str | None=None,limit: int=50):
            actor = self.actor(request)
            self.auth.recheck(actor,'read',None)
            require(type in (None,'user','service','group'))
            values = [dict(id=p['id'],type=p['type'],display_name=p['display_name']) for p in self.store.all('principal') if (type is None or type==p['type']) and q.casefold() in p['display_name'].casefold()]
            return self.page(values,cursor,limit)

        from .access import register_access
        from .admin import register_admin
        register_access(self)
        register_admin(self)
