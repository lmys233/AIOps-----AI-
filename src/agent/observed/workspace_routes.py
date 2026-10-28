"""System workspaces expose artifacts through nested, scope-checked routes."""
from fastapi import Request
from typing import Literal
from fastapi.responses import JSONResponse
from .routes import guarded
from .workspace import authorize,store_of,session_body,incident_body,incidents


def install(app,service):
    from .legacy_history import install as install_legacy
    install_legacy(app,service)
    root='/v1/observed-systems/{oid}'
    def run(fn):return guarded(service.observed_catalog,fn)
    @app.get(root+'/sessions')
    async def listing(oid:str,limit:int=50):return await run(lambda: list_body(service,oid,limit))
    @app.post(root+'/sessions')
    async def create(oid:str,request:Request,response_mode:Literal['full','view']='full',include_history:bool=True):
        return await run(lambda: create_body(service,oid,request,response_mode,include_history))
    @app.get(root+'/sessions/last')
    async def last(oid:str,response_mode:Literal['full','view']='full',include_history:bool=True):
        return await run(lambda: last_body(service,oid,response_mode,include_history))
    @app.get(root+'/sessions/{sid}')
    async def detail(oid:str,sid:str,before:str|None=None,response_mode:Literal['full','view']='full'):
        return await run(lambda: session_body(service,oid,sid,before,response_mode))
    @app.get(root+'/sessions/{sid}/tasks')
    async def tasks(oid:str,sid:str):return await run(lambda: task_body(service,oid,sid))
    @app.get(root+'/sessions/{sid}/tasks/{tid}/execution')
    async def execution(oid:str,sid:str,tid:str,after:str='',limit:int=20):
        from ..execution.view import page
        return await run(lambda: page(service,oid,sid,tid,after,limit))
    @app.patch(root+'/sessions/{sid}')
    async def rename(oid:str,sid:str,request:Request,response_mode:Literal['full','view']='full',include_history:bool=True):
        return await run(lambda: rename_body(service,oid,sid,request,response_mode,include_history))
    @app.delete(root+'/sessions/{sid}')
    async def delete(oid:str,sid:str):return await run(lambda: delete_body(service,oid,sid))
    @app.post(root+'/sessions/{sid}/select')
    async def select(oid:str,sid:str,response_mode:Literal['full','view']='full',include_history:bool=True):
        return await run(lambda: select_body(service,oid,sid,response_mode,include_history))
    @app.get(root+'/incidents')
    async def listing_incidents(oid:str,limit:int=50):return await run(lambda: incidents(service,oid,limit))
    @app.get(root+'/alerts')
    async def alert_receipts(oid:str):
        async def read():
            tenant,_=await authorize(service,oid)
            from ..alerts.receipts import store_of as alert_store
            rows=await alert_store(service).page(tenant,oid)
            return {'alerts':rows,'observed_id':oid,
                'delivery_semantics':'状态记录可持久化；后台队列在进程内，旧 queued/running 不证明此刻正在执行，也不保证自动恢复'}
        return await run(read)
    @app.get(root+'/incidents/{iid}')
    async def detail_incident(oid:str,iid:str,response_mode:Literal['full','view']='full'):
        return await run(lambda: saved_view(service,oid,iid) if response_mode=='view' else incident_body(service,oid,iid))
    @app.get(root+'/incidents/{iid}/payload/{part}')
    async def payload(oid:str,iid:str,part:str,cursor:str|None=None,limit:int=20,version:str|None=None):
        return await run(lambda: payload_page(service,oid,iid,part,cursor,limit,version))
    @app.get(root+'/capabilities')
    async def caps(oid:str):return await run(lambda: capabilities(service,oid))
    @app.get(root+'/incidents/{iid}/evidence')
    async def evidence_index(oid:str,iid:str):return await run(lambda: evidence_body(service,oid,iid))
    @app.get(root+'/incidents/{iid}/evidence/{eid}')
    async def evidence_detail(oid:str,iid:str,eid:str):return await run(lambda: evidence_body(service,oid,iid,eid))


async def list_body(service,oid,limit):
    tenant,_=await authorize(service,oid)
    return {'sessions':await store_of(service).listing(tenant,oid,limit),'observed_id':oid}


async def task_body(service,oid,sid):
    tenant,_=await authorize(service,oid);await store_of(service).owned(tenant,oid,sid)
    from ..adapters.task_runtime import controller
    from ..domain.tasks import Scope
    tasks,meta=await controller(service).repository.load(Scope(tenant=tenant,observed_id=oid,session_id=sid,env='prod'))
    fields={'task_id','purpose','state','revision','missing','slots','problem_id','candidate_id',
        'diagnosis_id','last_incident_id','used','selected_plan_id','budget_unknown','execution_id','memory'}
    return {'observed_id':oid,'foreground_task_id':meta['foreground'],
        'tasks':[t.model_dump(mode='json',include=fields) for t in tasks]}


async def create_body(service,oid,request,mode='full',include_history=True):
    tenant,row=await authorize(service,oid)
    if not row['active']:raise PermissionError('system disabled')
    data=await request.json();title=str(data.get('title') or '')[:160]
    record=service.sessions.create(title=title);await store_of(service).bind(tenant,oid,record.session_id,title)
    body=await session_body(service,oid,record.session_id,mode=mode,include_history=include_history)
    return body


async def last_body(service,oid,mode='full',include_history=True):
    body=await list_body(service,oid,100);items=body['sessions']
    if not items:raise KeyError('no system sessions yet')
    selected=[r for r in items if r.get('selected_at')]
    chosen=max(selected,key=lambda r:r['selected_at']) if selected else items[0]
    return await session_body(service,oid,chosen['session_id'],mode=mode,include_history=include_history)


async def rename_body(service,oid,sid,request,mode='full',include_history=True):
    tenant,_=await authorize(service,oid);data=await request.json();title=str(data.get('title') or '').strip()
    if not title or len(title)>160:raise ValueError('invalid title')
    await store_of(service).update_view(tenant,oid,sid,title=title)
    return await session_body(service,oid,sid,mode=mode,include_history=include_history)


async def select_body(service,oid,sid,mode='full',include_history=True):
    tenant,_=await authorize(service,oid);await store_of(service).update_view(tenant,oid,sid,select=True)
    return await session_body(service,oid,sid,mode=mode,include_history=include_history)


async def delete_body(service,oid,sid):
    tenant,_=await authorize(service,oid);await store_of(service).remove(tenant,oid,sid)
    from .context import Scope,use
    with use(Scope(service.observed_catalog,tenant,oid)):
        memory=getattr(service.orchestrator,'session',None)
        if memory:await memory.clear(sid)
        if service.history.cache:
            from .context import session_key
            await service.history.cache.invalidate(session_key(sid))
    return {'deleted':True,'session_id':sid,'observed_id':oid}


async def capabilities(service,oid):
    tenant,row=await authorize(service,oid)
    from webui.server import build_capabilities
    data=build_capabilities();data['observed_system']={k:row[k] for k in ('observed_id','name','active')}
    data['registered_services']=await service.observed_catalog.services(tenant,oid)
    return data


async def evidence_body(service,oid,iid,eid=None):
    from ..presentation.references import index,lookup
    body=await incident_body(service,oid,iid)
    if eid is None:return {'incident_id':iid,'references':index(body),'content_source':'saved_incident'}
    result=lookup(body,eid)
    if result is None:raise KeyError('evidence not in incident')
    return result


async def saved_view(service,oid,iid):
    await authorize(service,oid)
    from ..history.payloads import incident_view
    return await incident_view(service.history.store,oid,iid)


async def payload_page(service,oid,iid,part,cursor,limit,version):
    await authorize(service,oid)
    from ..history.payloads import page
    return await page(service.history.store,oid,iid,part,cursor=cursor,limit=limit,version=version)
