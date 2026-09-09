"""Independent service JWT and configured, normalized W3 verification."""
import time
from uuid import NAMESPACE_URL, uuid5

import httpx
import jwt

from .models import ApiError, actions, now, parse_time, require


def decode_service_token(token, config):
    try:
        claims = jwt.decode(token,config.jwt_secret,algorithms=['HS256'],issuer=config.issuer,audience=config.audience,
            options={'require':['sub','iss','aud','token_type','iat','exp','ver'],'strict_aud':True},leeway=0)
        require(claims['token_type'] == 'service' and type(claims['ver']) is int and type(claims['iat']) is int and type(claims['exp']) is int,
                401,'invalid_token','Invalid service token')
        require(claims['exp'] > claims['iat'] and claims['iat'] <= time.time(),401,'invalid_token','Invalid token lifetime')
        return claims
    except jwt.PyJWTError:
        raise ApiError(401,'invalid_token','Invalid or expired service token') from None


def issue_token(config, account, expires_in):
    require(type(expires_in) is int and 1 <= expires_in <= 86400*30,message='expires_in must be 1 to 2592000 seconds')
    require(account['enabled'],409,'account_disabled','Service account is disabled')
    issued = int(time.time())
    return jwt.encode(dict(sub=account['id'],iss=config.issuer,aud=config.audience,token_type='service',iat=issued,exp=issued+expires_in,ver=account['token_version']),config.jwt_secret,algorithm='HS256')


class Auth:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.client = httpx.Client(timeout=8,follow_redirects=False,trust_env=False)

    def close(self):
        self.client.close()

    @staticmethod
    def bearer(header):
        require(isinstance(header,str) and header.startswith('Bearer ') and 0 < len(header[7:]) <= 16384,401,'unauthenticated','A bearer credential is required')
        return header[7:]

    def authenticate(self, token):
        try:
            claims = decode_service_token(token,self.config)
        except ApiError:
            # A token claiming this issuer is never retried as an employee token.
            try:
                unverified = jwt.decode(token,options={'verify_signature':False})
            except jwt.PyJWTError:
                unverified = {}
            if unverified.get('iss') == self.config.issuer or not self.config.w3_verify_url:
                raise ApiError(401,'invalid_token','Invalid or expired credential') from None
            return self.verify_human(token)
        account = self.store.get('account',claims['sub'])
        require(account and account['enabled'] and account['token_version'] == claims['ver'],401,'invalid_token','Service account disabled or token revoked')
        return dict(principal_id=account['id'],principal_type='service',display_name=account['display_name'],
                    scopes=account['scopes'],is_admin=False,expires=claims['exp'],token_version=claims['ver'],groups=[])

    def verify_human(self, token):
        require(bool(self.config.w3_verify_url),401,'human_auth_unavailable','Human authentication is not configured')
        try:
            response = self.client.post(self.config.w3_verify_url,headers={'Authorization':'Bearer '+token,'Accept':'application/json'},json={'audience':self.config.audience})
            if response.status_code in (401,403):
                raise ApiError(401,'invalid_token','Human credential was rejected')
            require(response.status_code == 200,503,'auth_unavailable','Human verification is unavailable')
            require(len(response.content) <= 65536,503,'auth_unavailable','Invalid verification response')
            data = response.json()
            require(isinstance(data,dict),503,'auth_unavailable','Invalid verification response')
            require(data.get('active') is True,401,'invalid_token','Human credential is not active')
            uid, name = data.get('user_id'), data.get('display_name')
            require(isinstance(uid,str) and 0 < len(uid) <= 191 and isinstance(name,str) and 0 < len(name) <= 200,503,'auth_unavailable','Invalid normalized identity')
            expiry = parse_time(data.get('expires_at'))
            require(expiry is not None and expiry > now(),401,'invalid_token','Human credential has expired')
            groups = data.get('groups',[])
            require(isinstance(groups,list) and len(groups) <= 500 and all(isinstance(g,str) and 0 < len(g) <= 191 for g in groups),503,'auth_unavailable','Invalid normalized groups')
        except (httpx.HTTPError,ValueError):
            raise ApiError(503,'auth_unavailable','Human verification is unavailable') from None
        principal_id = str(uuid5(NAMESPACE_URL,'aresclaw-dashboard:w3:user:'+uid))
        group_ids = [str(uuid5(NAMESPACE_URL,'aresclaw-dashboard:w3:group:'+group)) for group in groups]
        with self.store.engine.begin() as connection:
            self.store.put('principal',dict(id=principal_id,type='user',display_name=name,external_user_id=uid),connection)
            for ident, group in zip(group_ids,groups):
                self.store.put('principal',dict(id=ident,type='group',display_name=group,external=True),connection)
        return dict(principal_id=principal_id,principal_type='human',display_name=name,scopes=['read','write','manage'],
                    is_admin=uid in self.config.admin_user_ids,expires=expiry.timestamp(),groups=group_ids)

    def recheck(self, actor, scope, connection):
        require(actor['expires'] > time.time(),401,'invalid_token','Credential has expired')
        if actor['principal_type'] == 'service':
            account = self.store.get('account',actor['principal_id'],connection)
            require(account and account['enabled'] and account['token_version'] == actor['token_version'],401,'invalid_token','Service account disabled or token revoked')
            actor['scopes'] = account['scopes']
        require(scope in actor['scopes'],403,'insufficient_scope','Credential lacks required scope')

    def effective_access(self, dashboard, actor, connection=None):
        if dashboard['owner_principal_id'] == actor['principal_id'] and dashboard['owner_type'] == actor['principal_type']:
            return dict(role='owner',sources=[{'type':'owner'}],expires_at=None,allowed_actions=actions('owner'))
        if dashboard['status'] == 'archived':
            return dict(role=None,sources=[],expires_at=None,allowed_actions=[])
        groups = set(actor.get('groups',[]))
        if actor['principal_type'] == 'human':
            groups.update(g['id'] for g in self.store.all('group',connection) if actor['principal_id'] in g['members'])
        sources = []
        for grant in dashboard.get('grants',[]):
            typ, subject = grant['subject_type'],grant['subject_id']
            applies = ((typ == 'service' and actor['principal_type'] == 'service' and subject == actor['principal_id']) or
                       (typ == 'user' and actor['principal_type'] == 'human' and subject == actor['principal_id']) or
                       (typ == 'group' and actor['principal_type'] == 'human' and subject in groups) or
                       (typ == 'all_authenticated' and actor['principal_type'] == 'human'))
            start, end = parse_time(grant.get('starts_at')), parse_time(grant.get('expires_at'))
            if applies and (start is None or start <= now()) and (end is None or now() < end):
                sources.append(grant)
        role = 'editor' if any(g['role'] == 'editor' for g in sources) else 'viewer' if sources else None
        relevant = [g for g in sources if g['role'] == role]
        expiry = None if any(g.get('expires_at') is None for g in relevant) else max((g['expires_at'] for g in relevant),default=None)
        return dict(role=role,sources=sources,expires_at=expiry,allowed_actions=actions(role))

    def authorize(self, dashboard, actor, action='read', connection=None):
        self.recheck(actor,action,connection)
        access = self.effective_access(dashboard,actor,connection)
        require(access['role'] is not None,404,'not_found','Dashboard is not visible')
        require(action in access['allowed_actions'],403,'forbidden','This action requires a higher dashboard role')
        return access
