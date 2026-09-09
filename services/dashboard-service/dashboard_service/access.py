"""Per-dashboard grants and human-only local groups."""
from fastapi import Body, Request

from .models import identifier, parse_time, require, revision, text_field, timestamp


def validate_grant(api, data, connection):
    typ, ident, role = data.get('subject_type'),data.get('subject_id'),data.get('role')
    require(typ in ('user','service','group','all_authenticated') and isinstance(ident,str) and role in ('viewer','editor'))
    if typ == 'all_authenticated':
        require(role=='viewer' and ident=='*',message='Public access is human viewer access only')
    else:
        principal = api.store.get('principal',ident,connection)
        require(principal and principal['type']==typ,message='Subject must be selected from the principal directory')
    start, end = parse_time(data.get('starts_at')),parse_time(data.get('expires_at'))
    require(not (start and end) or start<end,message='expires_at must be after starts_at')
    return dict(subject_type=typ,subject_id=ident,role=role,starts_at=timestamp(start) if start else None,expires_at=timestamp(end) if end else None)


def register_access(api):
    router = api.router

    def apply_change(dashboard, data, connection):
        action = data.get('action')
        if action == 'grant':
            grant = validate_grant(api,data,connection)
            dashboard['grants'] = [g for g in dashboard['grants'] if (g['subject_type'],g['subject_id'])!=(grant['subject_type'],grant['subject_id'])]+[grant]
        elif action == 'revoke':
            require(data.get('subject_type') in ('user','service','group','all_authenticated') and isinstance(data.get('subject_id'),str))
            dashboard['grants'] = [g for g in dashboard['grants'] if (g['subject_type'],g['subject_id'])!=(data['subject_type'],data['subject_id'])]
        elif action == 'set_public':
            require(type(data.get('enabled')) is bool)
            dashboard['grants'] = [g for g in dashboard['grants'] if g['subject_type']!='all_authenticated']
            if data['enabled']:
                dashboard['grants'].append(validate_grant(api,data | dict(subject_type='all_authenticated',subject_id='*',role='viewer'),connection))
        else:
            require(False,message='Unknown access change action')

    @router.get('/dashboards/{ident}/grants')
    def grants(ident: str,request: Request):
        _,dashboard = api.read(request,ident,'manage')
        return dict(items=dashboard['grants'],revision=dashboard['revision'])

    @router.post('/dashboards/{ident}/grants')
    def grant(ident: str,request: Request,payload: dict=Body(...)):
        return api.change_dashboard(request,ident,payload,lambda d,c:apply_change(d,payload | {'action':'grant'},c))

    @router.delete('/dashboards/{ident}/grants/{subject_type}/{subject_id}')
    def revoke(ident: str,subject_type: str,subject_id: str,request: Request,expected_revision: int):
        payload = dict(subject_type=subject_type,subject_id=subject_id,expected_revision=expected_revision,action='revoke')
        return api.change_dashboard(request,ident,payload,lambda d,c:apply_change(d,payload,c))

    @router.put('/dashboards/{ident}/public-access')
    def public_access(ident: str,request: Request,payload: dict=Body(...)):
        return api.change_dashboard(request,ident,payload,lambda d,c:apply_change(d,payload | {'action':'set_public'},c))

    @router.post('/dashboards/{ident}/access-changes')
    def access_changes(ident: str,request: Request,payload: dict=Body(...)):
        require(isinstance(payload.get('changes'),list) and 1 <= len(payload['changes']) <= 50)
        def apply(dashboard,connection):
            for change in payload['changes']:
                require(isinstance(change,dict))
                apply_change(dashboard,change,connection)
        return api.change_dashboard(request,ident,payload,apply)

    @router.get('/dashboards/{ident}/access')
    def access(ident: str,request: Request,subject_id: str | None=None):
        actor,dashboard = api.read(request,ident)
        target = actor
        if subject_id and subject_id != actor['principal_id']:
            api.auth.authorize(dashboard,actor,'manage')
            principal = api.store.get('principal',subject_id)
            require(principal and principal['type'] in ('user','service'),404,'not_found','Principal is not visible')
            target = dict(principal_id=subject_id,principal_type='human' if principal['type']=='user' else 'service',groups=[])
        result = api.auth.effective_access(dashboard,target)
        if target is not actor and target['principal_type']=='human':
            # An arbitrary user's fresh external W3 groups cannot be inferred from a directory lookup.
            result['external_groups_checked'] = False
        return result

    @router.get('/groups')
    def groups(request: Request,q: str='',cursor: str | None=None,limit: int=50):
        actor = api.actor(request)
        api.auth.recheck(actor,'read',None)
        return api.page([g for g in api.store.all('group') if q.casefold() in g['display_name'].casefold()],cursor,limit)

    @router.post('/groups')
    def create_group(request: Request,payload: dict=Body(...)):
        actor = api.actor(request)
        require(actor['principal_type']=='human',403,'human_required','Local group management requires a human identity')
        name = text_field(payload,'display_name',200,True)
        def apply(connection,operation_id):
            group = dict(id=identifier(),display_name=name,owner_principal_id=actor['principal_id'],members=[],revision=1,created_at=timestamp())
            api.store.put('group',group,connection)
            api.store.put('principal',dict(id=group['id'],display_name=name,type='group'),connection)
            return group | {'operation_id':operation_id}
        return api.mutation(request,actor,payload,apply)

    @router.put('/groups/{ident}/members')
    def members(ident: str,request: Request,payload: dict=Body(...)):
        actor = api.actor(request)
        require(actor['principal_type']=='human',403,'human_required','Local group management requires a human identity')
        def apply(connection,operation_id):
            group = api.store.get('group',ident,connection)
            require(group and (group['owner_principal_id']==actor['principal_id'] or actor['is_admin']),404,'not_found','Group is not manageable')
            revision(group,payload.get('expected_revision'))
            members = payload.get('members')
            require(isinstance(members,list) and len(members)<=1000)
            for member in members:
                require(isinstance(member,str))
                principal = api.store.get('principal',member,connection)
                require(principal and principal['type']=='user',message='Group members must be directory users')
            group.update(members=sorted(set(members)),revision=group['revision']+1)
            api.store.put('group',group,connection)
            return group | {'operation_id':operation_id}
        return api.mutation(request,actor,payload,apply)
