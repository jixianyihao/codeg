"""MySQL transactions and immutable files; every write locks one quota row.

The lock deliberately serializes metadata commits in this modest shared service.
It coordinates account revocation, ACL, quotas and idempotency across processes.
HTML upload/verification happens before the lock; final authorization happens inside.
"""
import hashlib
import os
import time
from contextlib import contextmanager

from sqlalchemy import Column, Integer, JSON, MetaData, String, Table, UniqueConstraint, create_engine, delete, insert, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert

from .models import ApiError, canonical, identifier, require, timestamp

metadata = MetaData()
records = Table('dashboard_records', metadata,
    Column('kind', String(32, collation='utf8mb4_bin'), primary_key=True),
    Column('id', String(191, collation='utf8mb4_bin'), primary_key=True), Column('body', JSON, nullable=False))
operations = Table('dashboard_operations', metadata,
    Column('id', String(36), primary_key=True), Column('principal_id', String(191, collation='utf8mb4_bin'), nullable=False),
    Column('request_id', String(128, collation='utf8mb4_bin'), nullable=False),
    Column('fingerprint', String(64), nullable=False), Column('body', JSON, nullable=False),
    UniqueConstraint('principal_id', 'request_id', name='uq_dashboard_operation_principal_key'))
locks = Table('dashboard_locks', metadata, Column('id', Integer, primary_key=True), Column('schema_version', Integer, nullable=False))
audit = Table('dashboard_audit', metadata, Column('id', String(36), primary_key=True), Column('body', JSON, nullable=False))


class Store:
    def __init__(self, config):
        self.config = config.validate()
        self.engine = create_engine(config.database_url, pool_pre_ping=True, isolation_level='READ COMMITTED',
                                    connect_args={'connect_timeout':5, 'read_timeout':30, 'write_timeout':30})
        self.root = config.storage_dir.resolve()

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.execute(mysql_insert(locks).values(id=1, schema_version=1).prefix_with('IGNORE'))
            version = connection.execute(select(locks.c.schema_version).where(locks.c.id == 1)).scalar_one()
            require(version == 1, 503, 'schema_mismatch', 'Unsupported database schema')

    @contextmanager
    def transaction(self):
        with self.engine.begin() as connection:
            connection.execute(select(locks).where(locks.c.id == 1).with_for_update()).one()
            yield connection

    def close(self):
        self.engine.dispose()

    def get(self, kind, ident, connection=None):
        if connection is None:
            with self.engine.connect() as connection:
                return self.get(kind, ident, connection)
        return connection.execute(select(records.c.body).where(records.c.kind == kind, records.c.id == ident)).scalar_one_or_none()

    def all(self, kind, connection=None):
        if connection is None:
            with self.engine.connect() as connection:
                return self.all(kind, connection)
        return list(connection.execute(select(records.c.body).where(records.c.kind == kind).order_by(records.c.id)).scalars())

    def put(self, kind, value, connection):
        statement = mysql_insert(records).values(kind=kind, id=value['id'], body=value)
        connection.execute(statement.on_duplicate_key_update(body=statement.inserted.body))

    def remove(self, kind, ident, connection):
        connection.execute(delete(records).where(records.c.kind == kind, records.c.id == ident))

    def dashboard(self, ident, connection=None):
        result = self.get('dashboard', ident, connection)
        require(result is not None, 404, 'not_found', 'Dashboard is not visible')
        return result

    def account(self, ident, connection=None):
        result = self.get('account', ident, connection)
        require(result is not None, 404, 'not_found', 'Account not found')
        return result

    def create_account(self, name, scopes, connection=None):
        if connection is None:
            with self.transaction() as connection:
                return self.create_account(name, scopes, connection)
        require(isinstance(name, str) and 0 < len(name.strip()) <= 200, message='A display name is required')
        self.validate_scopes(scopes)
        account = dict(id=identifier(),display_name=name,scopes=scopes,enabled=True,token_version=1,revision=1,created_at=timestamp())
        self.put('account',account,connection)
        self.put('principal',dict(id=account['id'],type='service',display_name=name),connection)
        self.record_audit('operator', 'create-account', account['id'], connection)
        return account

    @staticmethod
    def validate_scopes(scopes):
        require(isinstance(scopes,list) and all(x in ('read','write','manage') for x in scopes), message='Scopes must be read, write or manage')

    def update_account(self, ident, changes, connection=None):
        if connection is None:
            with self.transaction() as connection:
                return self.update_account(ident,changes,connection)
        account = self.account(ident,connection)
        if 'scopes' in changes:
            self.validate_scopes(changes['scopes']); account['scopes'] = changes['scopes']
        if 'enabled' in changes:
            require(type(changes['enabled']) is bool)
            if account['enabled'] and not changes['enabled']:
                account['token_version'] += 1
            account['enabled'] = changes['enabled']
        if 'display_name' in changes:
            name = changes['display_name']
            require(isinstance(name,str) and 0 < len(name.strip()) <= 200)
            account['display_name'] = name
            self.put('principal',dict(id=ident,type='service',display_name=name),connection)
        account['revision'] += 1
        self.put('account',account,connection)
        self.record_audit('operator', 'update-account', ident, connection)
        return account

    def reset_account(self, ident, connection=None):
        if connection is None:
            with self.transaction() as connection:
                return self.reset_account(ident,connection)
        account = self.account(ident,connection)
        account['token_version'] += 1
        account['revision'] += 1
        self.put('account',account,connection)
        self.record_audit('operator','reset-token-version',ident,connection)
        return account

    def record_audit(self, principal, action, target, connection, operation_id=None):
        connection.execute(insert(audit).values(id=identifier(),body=dict(principal_id=principal,action=action,target=target,operation_id=operation_id,created_at=timestamp())))

    def operation(self, principal, *, operation_id=None, request_id=None, connection=None):
        if connection is None:
            with self.engine.connect() as connection:
                return self.operation(principal,operation_id=operation_id,request_id=request_id,connection=connection)
        query = select(operations).where(operations.c.principal_id == principal)
        query = query.where(operations.c.id == operation_id) if operation_id else query.where(operations.c.request_id == request_id)
        row = connection.execute(query).mappings().one_or_none()
        return dict(row) if row else None

    def mutate(self, actor, key, method, path, payload, action, final_check):
        require(isinstance(key,str) and 1 <= len(key) <= 128 and key.isascii() and all(32 < ord(x) < 127 for x in key), message='Idempotency-Key is required (1-128 printable ASCII characters)')
        fingerprint = hashlib.sha256(canonical([method,path,payload]).encode()).hexdigest()
        with self.transaction() as connection:
            final_check(connection)
            previous = self.operation(actor['principal_id'],request_id=key,connection=connection)
            if previous:
                require(previous['fingerprint'] == fingerprint,409,'idempotency_conflict','Idempotency key was used for a different request')
                return previous['body']['result']
            # Stable-principal rate limit survives token changes and process restarts.
            rate = self.get('rate',actor['principal_id'],connection) or dict(id=actor['principal_id'],times=[])
            rate['times'] = [t for t in rate['times'] if t > time.time()-60]
            require(len(rate['times']) < self.config.writes_per_minute,429,'rate_limited','Write limit exceeded; retry later')
            operation_id = identifier()
            result = action(connection,operation_id)
            self.put('rate',rate | {'times':rate['times']+[time.time()]},connection)
            connection.execute(insert(operations).values(id=operation_id,principal_id=actor['principal_id'],request_id=key,fingerprint=fingerprint,
                body=dict(operation_id=operation_id,state='succeeded',result=result,created_at=timestamp())))
            self.record_audit(actor['principal_id'],method+' '+path,result.get('dashboard_id') or result.get('id'),connection,operation_id)
            return result

    def write_content(self, version_id, content):
        # Generated UUID only; filenames and user input never influence storage paths.
        path = self.root / (version_id+'.html')
        with path.open('xb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        return path

    def read_content(self, version):
        path = self.root / (version['id']+'.html')
        try:
            content = path.read_bytes()
        except OSError:
            raise ApiError(503,'content_unavailable','Content storage is unavailable') from None
        require(len(content) == version['byte_size'] and hashlib.sha256(content).hexdigest() == version['sha256'],503,'content_corrupt','Content integrity check failed')
        return content

    def verify_storage(self):
        with self.transaction() as connection:
            versions = self.all('version',connection)
            for version in versions:
                self.read_content(version)
            referenced = {v['id']+'.html' for v in versions}
            # Only old, unreferenced UUID files beneath this exact content directory.
            from uuid import UUID
            for path in self.root.glob('*.html'):
                try:
                    UUID(path.stem)
                except ValueError:
                    continue
                if path.name not in referenced and path.stat().st_mtime < time.time()-86400 and path.resolve().parent == self.root:
                    path.unlink()
        return {'verified_versions':len(versions)}
