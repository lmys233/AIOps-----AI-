"""Scope-checked warning list; receipts remain the persistent source of truth."""
from .query import store_of,cursor_encode,cursor_decode
from ..observed.workspace import authorize
from ..observed.context import Scope
from ..observed.routes import guarded
from pydantic import BaseModel,ConfigDict,Field,StrictInt


class Reviewed(BaseModel):
    model_config=ConfigDict(extra='forbid')
    snapshot:StrictInt=Field(ge=0,lt=2**63)
    env:str='prod'
    cluster:str|None=None


async def scope_for(service,oid,env,cluster):
    tenant,_=await authorize(service,oid)
    return Scope(service.observed_catalog,tenant,oid,env,cluster or None)


def install(app,service):
    root='/v1/observed-systems/{oid}/warnings'
    def run(fn):return guarded(service.observed_catalog,fn)

    @app.get(root)
    async def listing(oid:str,env:str='prod',cluster:str|None=None,cursor:str='',limit:int=20):
        async def read():
            scope=await scope_for(service,oid,env,cluster)
            options=cursor_decode(scope,cursor) if cursor else {}
            result=await store_of(service).page(scope,where=options.get('where'),snapshot=options.get('snapshot'),before=options.get('before'),limit=limit)
            result['next_cursor']=cursor_encode(scope,result,options.get('where'))
            return result
        return await run(read)

    @app.get(root+'/count')
    async def count(oid:str,env:str='prod',cluster:str|None=None):
        async def read():
            scope=await scope_for(service,oid,env,cluster)
            return await store_of(service).counts(scope)
        return await run(read)

    @app.post(root+'/review')
    async def review(oid:str,payload:Reviewed):
        async def save():
            scope=await scope_for(service,oid,payload.env,payload.cluster)
            return await store_of(service).review(scope,payload.snapshot)
        return await run(save)

    @app.get(root+'/{warning_id}')
    async def detail(oid:str,warning_id:str,env:str='prod',cluster:str|None=None,snapshot:int|None=None):
        async def read():
            scope=await scope_for(service,oid,env,cluster)
            result=await store_of(service).page(scope,where=dict(field='warning_id',op='eq',value=warning_id),limit=1,snapshot=snapshot,include_record=True)
            if not result['items']:raise KeyError('告警不存在于当前系统/环境/集群')
            return result['items'][0]
        return await run(read)
