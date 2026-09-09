"""W3 human administrator endpoints. Machine JWTs never obtain admin authority."""
from fastapi import Body, Request

from .auth import issue_token
from .models import require, revision, text_field


def register_admin(api):
    router = api.router

    def admin(request):
        actor = api.actor(request)
        require(actor['principal_type']=='human' and actor['is_admin'],403,'admin_required','A registered human administrator is required')
        return actor

    @router.get('/admin/service-accounts')
    def accounts(request: Request,cursor: str | None=None,limit: int=50):
        admin(request)
        return api.page(api.store.all('account'),cursor,limit)

    @router.post('/admin/service-accounts')
    def create_account(request: Request,payload: dict=Body(...)):
        actor = admin(request)
        return api.mutation(request,actor,payload,lambda c,o:api.store.create_account(text_field(payload,'display_name',200,True),payload.get('scopes',['read']),c) | {'operation_id':o})

    @router.patch('/admin/service-accounts/{ident}')
    def update_account(ident: str,request: Request,payload: dict=Body(...)):
        actor = admin(request)
        require(set(payload)<={'expected_revision','display_name','scopes','enabled'})
        def apply(connection,operation_id):
            revision(api.store.account(ident,connection),payload.get('expected_revision'))
            return api.store.update_account(ident,payload,connection) | {'operation_id':operation_id}
        return api.mutation(request,actor,payload,apply)

    @router.post('/admin/service-accounts/{ident}/reset-token-version')
    def reset_account(ident: str,request: Request,payload: dict=Body(...)):
        actor = admin(request)
        def apply(connection,operation_id):
            revision(api.store.account(ident,connection),payload.get('expected_revision'))
            return api.store.reset_account(ident,connection) | {'operation_id':operation_id}
        return api.mutation(request,actor,payload,apply)

    @router.post('/admin/service-accounts/{ident}/tokens')
    def create_token(ident: str,request: Request,payload: dict=Body(...)):
        actor = admin(request)
        issued = []
        def apply(connection,operation_id):
            account = api.store.account(ident,connection)
            revision(account,payload.get('expected_revision'))
            lifetime = payload.get('expires_in',3600)
            issued.append(issue_token(api.config,account,lifetime))
            # Never persist credentials in an operation result, audit, URL or log.
            return dict(operation_id=operation_id,account_id=ident,token_version=account['token_version'],expires_in=lifetime,token_retrievable=False)
        result = api.mutation(request,actor,payload,apply)
        require(bool(issued),409,'token_already_issued','This issuance committed. Tokens cannot be retrieved; issue a new token with a new request key')
        return result | {'token':issued[0],'token_type':'Bearer'}

    @router.post('/admin/dashboards/{ident}/takeover')
    def takeover(ident: str,request: Request,payload: dict=Body(...)):
        actor = admin(request)
        require(isinstance(payload.get('reason'),str) and 1 <= len(payload['reason']) <= 1000,message='An audit reason is required')
        def apply(connection,operation_id):
            dashboard = api.store.dashboard(ident,connection)
            revision(dashboard,payload.get('expected_revision'))
            principal = api.store.get('principal',payload.get('owner_principal_id',''),connection)
            require(principal and principal['type'] in ('user','service'),message='New owner must be a directory principal')
            from .models import timestamp
            dashboard.update(owner_principal_id=principal['id'],owner_name=principal['display_name'],owner_type='human' if principal['type']=='user' else 'service',
                             revision=dashboard['revision']+1,updated_at=timestamp(),status='archived')
            api.store.put('dashboard',dashboard,connection)
            api.store.record_audit(actor['principal_id'],'takeover: '+payload['reason'],ident,connection,operation_id)
            return dict(operation_id=operation_id,dashboard_id=ident,revision=dashboard['revision'],status='archived')
        return api.mutation(request,actor,payload,apply)
