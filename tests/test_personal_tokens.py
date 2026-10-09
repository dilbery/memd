import json
import time
import uuid

import pytest
import respx

from memd.registry import Registry, ControlError, Conflict, Denied, principal
from memd.identity import current_identity, clear_identity
from tests.test_user_web import userweb, sign_in, headers
from tests.test_user_memory import stores
from tests.test_admin_web import key
from tests.test_control import registry


@pytest.fixture
def directory(tmp_path, monkeypatch):
    path=tmp_path/'directory.json'
    data={'generated':time.time(),'directory':[
        {'email':'alice@example.invalid','subject':'alice-sub','active':True},
        {'email':'bob@example.invalid','subject':'bob-sub','active':True}]}
    path.write_text(json.dumps(data));monkeypatch.setenv('MEMD_KASM_USER_MAP',str(path))
    yield path,data
    clear_identity();principal.set(None)


def issue(r,**kw):
    return r.issue_personal(subject='alice-sub',store='alice@example.invalid',label='My laptop',operation_id=str(uuid.uuid4()),**kw)


def test_personal_registry_ownership_lifecycle_and_admin_rotation(registry,directory):
    item=issue(registry);token=item['token'];secret=item['secret']
    assert token['personal_subject']=='alice-sub' and token['stores']==['alice@example.invalid']
    assert set(token['operations'])=={'read','recall','save'}
    assert registry.personal_list('bob-sub','bob@example.invalid')==[]
    assert secret not in json.dumps(registry.list())+json.dumps(registry.events())
    assert registry.authorize(token['id'],'save',None)=='alice@example.invalid'
    with pytest.raises(Denied):registry.authorize(token['id'],'read','bob@example.invalid')
    with pytest.raises(Denied):registry.authorize(token['id'],'reindex',None)
    with pytest.raises(Denied):registry.revoke_personal(token['id'],subject='bob-sub',store='bob@example.invalid',operation_id=str(uuid.uuid4()),revision=1)
    with pytest.raises(ControlError):registry.edit(token['id'],actor='admin',operation_id=str(uuid.uuid4()),revision=1,label='new',owner='bob@example.invalid',purpose='test')
    rotated=registry.rotate(token['id'],actor='admin',operation_id=str(uuid.uuid4()),revision=1,overlap_hours=0,days=30)
    assert rotated['token']['personal_subject']=='alice-sub'
    assert not registry.authenticate(secret)
    assert registry.authenticate(rotated['secret'])
    registry.revoke_personal(rotated['token']['id'],subject='alice-sub',store='alice@example.invalid',operation_id=str(uuid.uuid4()),revision=1)
    assert not registry.authenticate(rotated['secret'])


@pytest.mark.parametrize('change',['removed','inactive','reassigned','email_changed','stale','missing'])
def test_membership_and_subject_binding_fail_closed(registry,directory,change):
    path,data=directory;result=issue(registry)
    if change=='removed':data['directory']=[]
    elif change=='inactive':data['directory'][0]['active']=False
    elif change=='reassigned':data['directory'][0]['subject']='new-person'
    elif change=='email_changed':data['directory'][0]['email']='changed@example.invalid'
    elif change=='stale':data['generated']=time.time()-901
    if change=='missing':path.unlink()
    else:path.write_text(json.dumps(data))
    assert not registry.authenticate(result['secret'])
    with pytest.raises(Denied):registry.authorize(result['token']['id'],'read',None)
    with pytest.raises(Denied):issue(registry)


def test_caps_expiry_and_one_time_issue(registry,directory):
    with pytest.raises(ControlError):issue(registry,days=365)
    op=str(uuid.uuid4())
    registry.issue_personal(subject='alice-sub',store='alice@example.invalid',label='one',operation_id=op)
    with pytest.raises(Conflict):registry.issue_personal(subject='alice-sub',store='alice@example.invalid',label='one',operation_id=op)
    for _ in range(9):issue(registry)
    with pytest.raises(ControlError):issue(registry)


def test_personal_browser_token_to_rest_and_mcp_owner_boundary(userweb,key,directory,monkeypatch):
    client,r,app=userweb
    monkeypatch.setenv('MEMD_REQUIRE_RECALL_TOKEN','1')
    monkeypatch.setattr('memd.server._core_recall',lambda *a,**k: [])
    with respx.mock(assert_all_called=False) as router:
        sign_in(client,router,key);h=headers(client,r)
        assert client.get('/memories/onboarding').status_code==200
        url='/memories/onboarding/api/tokens'
        assert client.post(url,json={}).status_code==403
        body={'label':'Laptop <script>','days':30,'operation_id':str(uuid.uuid4())}
        assert client.post(url,headers=h,json={**body,'store':'bob@example.invalid'}).status_code==400
        response=client.post(url,headers=h,json=body);assert response.status_code==200
        result=response.json();token=result['token'];bearer={'Authorization':'Bearer '+result['secret']}
        assert result['secret'] not in client.get(url).text
        assert client.post(url,headers=h,json=body).status_code==409
        assert client.post('/recall',headers=bearer,json={'query':''}).json()['profile']=='alice@example.invalid'
        assert client.post('/recall',headers=bearer,json={'profile':'bob@example.invalid'}).status_code==403
        assert client.post('/reindex',headers=bearer,json={}).status_code==403
        # Remove browser cookies: bearer alone never grants either web console.
        cookies=dict(client.cookies);client.cookies.clear()
        assert client.get(url,headers=bearer).status_code==401
        assert client.get('/admin/api/tokens',headers=bearer).status_code==401
        assert client.post('/read',headers=bearer,json={'slug':'widget'}).json()['body'].startswith('Private widget for alice')
        assert client.post('/read',headers=bearer,json={'slug':'bob-only'}).status_code==404
        from memd.mcp_http import _authenticate
        from memd.mcp import _call_tool_sync
        assert _authenticate(bearer['Authorization']) and current_identity()=='alice@example.invalid'
        with pytest.raises(Denied):_call_tool_sync('read',{'slug':'widget','profile':'bob@example.invalid'})
        _authenticate(None);assert current_identity() is None
        client.cookies.update(cookies)
        revoked=client.post(url+'/'+token['id']+'/revoke',headers=h,json={'revision':1,'operation_id':str(uuid.uuid4())})
        assert revoked.status_code==200
        assert client.post('/recall',headers=bearer,json={}).status_code==401


def test_schema_upgrade_preserves_existing_authority_and_backups(registry,tmp_path):
    result=registry.issue(actor='admin',operation_id=str(uuid.uuid4()),label='automation',owner='team',purpose='test',stores=['jobs'],operations=['stats'])
    with registry.connection(write=True) as db:
        db.execute('ALTER TABLE tokens DROP COLUMN personal_subject')
        db.execute('ALTER TABLE tokens DROP COLUMN personal_store')
        db.execute("UPDATE metadata SET value='1' WHERE key='schema'")
    registry.upgrade();registry.upgrade()
    assert registry.authenticate(result['secret'])['id']==result['token']['id']
    registry.session_create({'kind':'admin'})
    backup=tmp_path/'backup.db';registry.backup(backup)
    restored=Registry(backup)
    assert restored.authenticate(result['secret'])
    with restored.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]==0
        assert db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0]=='2'
