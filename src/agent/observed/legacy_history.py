"""Unattributed reports remain readable without assigning them to a new system."""
import json,os
from .context import body_scope,enabled
from .workspace import store_of


async def reports(service):
    store=store_of(service);tenant=os.getenv('AIOPS_TENANT','default')
    rows=await store.fetch("""SELECT body FROM agent_chat_incidents WHERE tenant_id=$1
        AND body#>>'{explanation,observed_system,observed_id}' IS NULL ORDER BY incident_id DESC LIMIT 50""",tenant)
    bodies=[json.loads(r['body']) if isinstance(r['body'],str) else r['body'] for r in rows]
    for row in await service.list_incidents():
        if len(bodies)>=50:break
        body=await service.get_incident(row['incident_id'])
        if body and not body_scope(body) and body['incident_id'] not in {b['incident_id'] for b in bodies}:bodies.append(body)
    return {'incidents':[{'incident_id':b['incident_id'],'session_id':b['session_id'],'report':b.get('report',''),
        'status':b['status']} for b in bodies],'readonly':True,'limit':50}


async def block_unscoped_edit(service,sid):
    if not enabled() or not getattr(getattr(service,'history',None),'store',None):return None
    if not getattr(service.history.store,'dsn',None):return None
    store=store_of(service);tenant=os.getenv('AIOPS_TENANT','default')
    row=await store.one("""SELECT 1 FROM agent_system_sessions WHERE tenant=$1 AND session_id=$2
        UNION ALL SELECT 1 FROM agent_chat_turns WHERE tenant_id=$1 AND session_id=$2
        AND payload#>>'{response,explanation,observed_system,observed_id}' IS NOT NULL LIMIT 1""",tenant,sid)
    if row:
        from fastapi.responses import JSONResponse
        return JSONResponse({'code':'WORKSPACE_REQUIRED','message':'该会话已有系统归属；请使用对应系统工作台的会话接口'},403)


def install(app,service):
    from .routes import guarded
    @app.get('/v1/legacy-incidents')
    async def listing():return await guarded(service.observed_catalog,lambda: reports(service))
