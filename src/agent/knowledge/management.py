import asyncio
from fastapi import Request,Query
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from .models import DocumentInput,Mutation,Conflict
from .service import build_service
from .chunking import CHUNKERS


def install(app,service):
    plugin=getattr(service.router,'routing_plugin',None)
    provider=getattr(getattr(plugin,'index',None),'provider',None)
    knowledge=build_service(provider);service.knowledge=knowledge;app.state.knowledge=knowledge
    executor=getattr(service.orchestrator,'executor',None)
    if executor:executor.context.knowledge=knowledge

    async def start():
        if knowledge:knowledge.worker=asyncio.create_task(knowledge.recover())
    app.state.resources.start(start)
    if knowledge:
        owner=app.state.resources
        owner.own('knowledge workers',knowledge,method='stop_workers',worker=True)
        owner.own('knowledge store',knowledge.store)
        if knowledge.owns_provider:owner.own('knowledge embedding',knowledge.provider)

    @app.get('/v1/knowledge')
    async def listing(offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=50),
                      status:str=Query('',pattern=r'^(|review|active|inactive|deleted)$'),category:str='',q:str=Query('',max_length=200)):
        return await guarded(knowledge,lambda: knowledge.store.listing(offset,limit,status,category,q))

    @app.get('/v1/knowledge/config')
    async def config():
        if not knowledge:return JSONResponse({'error':'KNOWLEDGE_UNCONFIGURED'},503)
        return {'chunkers':list(CHUNKERS),'chunker':knowledge.chunker.identity,'embedding':knowledge.provider.identity,
                'embedding_kind':knowledge.provider.kind,'index_error':knowledge.last_error,
                'statuses':{'review':'审核','active':'生效','inactive':'失效','deleted':'删除'}}

    @app.post('/v1/knowledge')
    async def create(request:Request):return await guarded(knowledge,lambda: mutate(knowledge,service,request))

    @app.get('/v1/knowledge/{kid}')
    async def detail(kid:str,observed_id:str|None=None):
        return await guarded(knowledge,lambda: get(knowledge,service,kid,observed_id))

    @app.put('/v1/knowledge/{kid}')
    async def update(kid:str,request:Request):return await guarded(knowledge,lambda: mutate(knowledge,service,request,kid))

    @app.delete('/v1/knowledge/{kid}')
    async def delete(kid:str,revision:str=Query(min_length=1,max_length=128)):
        return await guarded(knowledge,lambda: knowledge.store.delete(kid,revision))

    @app.post('/v1/knowledge/{kid}/reindex')
    async def reindex(kid:str,revision:str=Query(min_length=1,max_length=128)):
        return await guarded(knowledge,lambda: rebuild(knowledge,kid,revision))

    @app.get('/v1/knowledge/{kid}/versions/{version}')
    async def version(kid:str,version:int,observed_id:str|None=None):
        return await guarded(knowledge,lambda: get(knowledge,service,kid,observed_id,version))


async def guarded(knowledge,operation):
    if not knowledge:return JSONResponse({'error':'KNOWLEDGE_UNCONFIGURED'},503)
    try:return await operation()
    except (ValidationError,ValueError) as exc:return JSONResponse({'error':'INVALID_KNOWLEDGE','message':str(exc)[:1000]},422)
    except Conflict as exc:return JSONResponse({'error':'KNOWLEDGE_REVISION_CONFLICT','message':str(exc)},409)
    except KeyError:return JSONResponse({'error':'KNOWLEDGE_NOT_FOUND'},404)
    except PermissionError:return JSONResponse({'error':'KNOWLEDGE_SCOPE_FORBIDDEN'},403)
    except Exception:return JSONResponse({'error':'KNOWLEDGE_DATABASE_UNAVAILABLE','message':'知识库操作未完成，请刷新核实'},503)


async def validate_scope(service,oid):
    if oid:
        from ..observed.workspace import authorize
        await authorize(service,oid)


async def mutate(knowledge,service,request,kid=None):
    body=await request.json();mutation=Mutation.model_validate(body) if kid else None
    data=mutation.document if mutation else DocumentInput.model_validate(body)
    await validate_scope(service,data.observed_id)
    result=await knowledge.store.mutate(data,kid,mutation.revision if mutation else None)
    knowledge.schedule(result['id']);return result


async def rebuild(knowledge,kid,revision):
    result=await knowledge.store.reindex(kid,revision);knowledge.schedule(kid);return result


async def get(knowledge,service,kid,oid,version=None):
    await validate_scope(service,oid);current=await knowledge.store.get(kid)
    if oid and current['observed_id'] not in (None,oid):raise PermissionError('another system')
    if version is None:return current
    result=await knowledge.store.version(kid,version)
    if oid and result.get('observed_id') not in (None,oid):raise PermissionError('version belongs to another system')
    return {**result,'current_status':current['status'],'historical_version':True}
