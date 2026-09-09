"""Real MySQL integration tests; never substitute SQLite for this suite."""
import hashlib
import json
import os
import secrets
import uuid
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def service(tmp_path):
    database_url = os.environ.get('TEST_DATABASE_URL')
    if not database_url:
        pytest.skip('TEST_DATABASE_URL must identify an isolated MySQL database')
    from dashboard_service.config import Config
    from dashboard_service.store import Store
    from dashboard_service.app import create_app
    from dashboard_service.auth import issue_token
    config = replace(Config.testing(), database_url=database_url, storage_dir=tmp_path)
    store = Store(config)
    store.initialize()
    accounts = [store.create_account(f'test-{uuid.uuid4()}', ['read','write','manage']) for _ in range(2)]
    tokens = [issue_token(config, a, 3600) for a in accounts]
    with TestClient(create_app(config, store=store)) as client:
        yield client, store, config, accounts, tokens
    store.close()


def headers(token, key=None):
    return {'Authorization': f'Bearer {token}', 'Idempotency-Key': key or str(uuid.uuid4())}


def publish(client, token, dashboard_id=None, revision=None, key=None, content=b'<h1>Verified content</h1>'):
    metadata = dict(title='Report',description='Example',byte_size=len(content),content_sha256=hashlib.sha256(content).hexdigest())
    if revision is not None:
        metadata['expected_revision'] = revision
    return client.post('/api/v1/dashboards' + (f'/{dashboard_id}/versions' if dashboard_id else ''), headers=headers(token,key),
                       files=[('metadata',(None,json.dumps(metadata),'application/json')),('html',('report.html',content,'text/html'))])


def test_private_access_signature_reset_disable_and_scope(service):
    client, store, config, accounts, tokens = service
    response = publish(client,tokens[0]); assert response.status_code == 200, response.text
    dashboard_id = response.json()['dashboard_id']
    for path in [f'/dashboards/{dashboard_id}', f'/dashboards/{dashboard_id}/versions', f'/dashboards/{dashboard_id}/grants']:
        assert client.get('/api/v1'+path,headers=headers(tokens[1])).status_code == 404
    store.update_account(accounts[0]['id'], {'scopes':['read']})
    assert publish(client,tokens[0],dashboard_id,1).status_code == 403
    store.reset_account(accounts[0]['id'])
    assert client.get('/api/v1/me',headers=headers(tokens[0])).status_code == 401
    from dashboard_service.auth import issue_token
    fresh = issue_token(config,store.account(accounts[0]['id']),3600)
    store.update_account(accounts[0]['id'], {'enabled':False})
    assert client.get('/api/v1/me',headers=headers(fresh)).status_code == 401
    store.update_account(accounts[0]['id'], {'enabled':True})
    assert client.get('/api/v1/me',headers=headers(fresh)).status_code == 401


def test_replay_conflict_immutable_versions_and_restart(service):
    client, store, config, _, tokens = service
    key = str(uuid.uuid4())
    first = publish(client,tokens[0],key=key); assert first.status_code == 200, first.text
    result = first.json(); ident = result['dashboard_id']
    assert publish(client,tokens[0],key=key).json() == result
    assert publish(client,tokens[0],key=key,content=b'changed').status_code == 409
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: publish(client,tokens[0],ident,1).status_code,range(2)))
    assert sorted(statuses) == [200,409]
    source = client.get(f'/api/v1/dashboards/{ident}/versions/{result["version_id"]}/source',headers=headers(tokens[0]))
    assert source.text == '<h1>Verified content</h1>'
    assert source.headers['content-type'].startswith('text/plain')
    assert 'attachment' in source.headers['content-disposition']
    from dashboard_service.store import Store
    reopened = Store(config); reopened.initialize(); reopened.verify_storage()
    assert reopened.dashboard(ident)['revision'] == 2
    reopened.close()
    operation = client.get('/api/v1/operations',params={'request_id':key},headers=headers(tokens[0])).json()
    assert operation['result'] == result
    assert client.get('/api/v1/operations/'+result['operation_id'],headers=headers(tokens[1])).status_code == 404


def test_grants_expiry_atomic_changes_and_archive(service):
    client, _, _, accounts, tokens = service
    result = publish(client,tokens[0]).json(); ident = result['dashboard_id']
    base = f'/api/v1/dashboards/{ident}'
    grant = dict(subject_type='service',subject_id=accounts[1]['id'],role='viewer',starts_at=None,expires_at=None,expected_revision=1)
    assert client.post(base+'/grants',headers=headers(tokens[0]),json=grant).status_code == 200
    assert client.get(base,headers=headers(tokens[1])).json()['role'] == 'viewer'
    assert publish(client,tokens[1],ident,2).status_code == 403
    bad = dict(expected_revision=2,changes=[dict(action='revoke',subject_type='service',subject_id=accounts[1]['id']),dict(action='grant',subject_type='user',subject_id='unknown',role='editor')])
    assert client.post(base+'/access-changes',headers=headers(tokens[0]),json=bad).status_code == 422
    assert client.get(base,headers=headers(tokens[1])).status_code == 200
    expired = grant | dict(expected_revision=2,expires_at='2020-01-01T00:00:00Z')
    assert client.post(base+'/grants',headers=headers(tokens[0]),json=expired).status_code == 200
    assert client.get(base,headers=headers(tokens[1])).status_code == 404
    assert client.post(base+'/archive',headers=headers(tokens[0]),json={'expected_revision':3}).status_code == 200
    assert publish(client,tokens[0],ident,4).status_code == 409
    assert client.post(base+'/restore',headers=headers(tokens[0]),json={'expected_revision':4}).status_code == 200


def test_machine_cannot_render_and_upload_validation(service):
    client, _, _, _, tokens = service
    result = publish(client,tokens[0]).json()
    assert client.post(f'/api/v1/dashboards/{result["dashboard_id"]}/view-capabilities',headers=headers(tokens[0]),json={}).status_code == 403
    assert publish(client,tokens[0],content=b'\xff').status_code == 422
    assert publish(client,tokens[0],content=secrets.token_bytes(10*1024*1024+1)).status_code == 413
