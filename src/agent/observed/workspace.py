"""Shared authority for workspace artifacts, independent from model query authorization."""
import os
from .workspace_store import WorkspaceStore
from .context import Scope,use


def store_of(service):
    if not getattr(service,'workspace_store',None):
        if not service.history:raise RuntimeError('durable chat archive not configured')
        service.workspace_store=WorkspaceStore(service.history.store.dsn)
        catalog=getattr(service,'observed_catalog',None)
        if catalog:catalog.store.children=getattr(catalog.store,'children',[])+[service.workspace_store]
    return service.workspace_store


async def authorize(service,oid):
    from diagnostics.observed import system_id
    system_id(oid);catalog=getattr(service,'observed_catalog',None)
    if not catalog:raise RuntimeError('registry unavailable')
    tenant=os.getenv('AIOPS_TENANT','default')
    row=await catalog.store.one('SELECT * FROM agent_observed_systems WHERE tenant=$1 AND observed_id=$2 AND registered_at IS NOT NULL',tenant,oid)
    if not row:raise KeyError('system not registered in tenant')
    return tenant,row


async def session_body(service,oid,sid,before=None,mode='full',include_history=True):
    from ..history.codec import full_view
    from ..api.history_pages import cursor
    tenant,row=await authorize(service,oid);store=store_of(service);await store.owned(tenant,oid,sid)
    sessions=await store.listing(tenant,oid,1,sid);record=next(iter(sessions),None)
    if not record:raise KeyError('session not in system')
    if not include_history:return {'session':record,'observed_id':oid}
    with use(Scope(service.observed_catalog,tenant,oid,observed_name=row['name'])):
        page=await service.history.page(sid,cursor(before),mode) if mode!='full' else await service.history.page(sid,cursor(before))
    from ..preferences.routes import overlay
    page=await overlay(service,oid,sid,page)
    return dict(page,session=record,turns=[full_view(t) for t in page['turns']])


async def incident_body(service,oid,iid):
    from .context import body_scope
    await authorize(service,oid);body=await service.get_incident(iid)
    if not body or body_scope(body)!=oid:raise KeyError('incident not in system')
    return body


async def incidents(service,oid,limit=50):
    tenant,_=await authorize(service,oid);store=store_of(service)
    rows=await store.fetch('''SELECT i.body FROM agent_chat_incidents i
        LEFT JOIN LATERAL (SELECT max(created_at) AS at FROM agent_chat_turns
            WHERE tenant_id=$1 AND incident_id=i.incident_id) t ON true
        WHERE i.tenant_id=$1 AND i.body#>>'{explanation,observed_system,observed_id}'=$2
        ORDER BY t.at DESC NULLS LAST,i.incident_id DESC LIMIT $3''',tenant,oid,min(max(limit,1),100))
    import json
    bodies=[json.loads(r['body']) if isinstance(r['body'],str) else r['body'] for r in rows]
    return {'incidents':[{'incident_id':b['incident_id'],'session_id':b['session_id'],'status':b['status'],
        'intent':b['intent']['name'],'rounds':b.get('rounds',0),'evidence_count':len(b.get('evidence',[])),'observed_id':oid} for b in bodies]}


async def chat_binding(service,tenant,oid,sid):
    store=store_of(service)
    row=await store.one('SELECT observed_id FROM agent_system_sessions WHERE tenant=$1 AND session_id=$2',tenant,sid)
    if row and row['observed_id']!=oid:raise ValueError('当前会话固定属于另一个系统；请在该系统工作台新建会话')
    view=await store.one('SELECT deleted FROM agent_system_session_views WHERE tenant=$1 AND observed_id=$2 AND session_id=$3',tenant,oid,sid)
    if view and view['deleted']:raise ValueError('该系统的会话已删除；请新建会话')
