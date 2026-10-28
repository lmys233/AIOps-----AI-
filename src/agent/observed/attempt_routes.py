"""Platform-authenticated scoped archive details, no SDK authentication material returned."""
import os
from .routes import guarded
from .attempt_store import AttemptStore


def install(app):
    @app.get('/v1/observed-systems/{oid}/access')
    async def page(oid:str,before:str|None=None,limit:int=20):
        return await guarded(app.state.observed_catalog,lambda: read(app,oid,before,limit))
    @app.get('/v1/observed-systems/{oid}/access/{aid}')
    async def detail(oid:str,aid:str):
        return await guarded(app.state.observed_catalog,lambda: read(app,oid,aid,1,detail=True))


async def read(app,oid,cursor,limit,detail=False):
    catalog=app.state.observed_catalog;tenant=os.getenv('AIOPS_TENANT','default')
    exists=await catalog.store.one('SELECT 1 FROM agent_observed_systems WHERE tenant=$1 AND observed_id=$2',tenant,oid)
    if not exists:raise KeyError('unknown system')
    if not hasattr(catalog,'attempts'):catalog.attempts=AttemptStore(catalog.store.dsn)
    if detail:return await catalog.attempts.detail(tenant,oid,cursor)
    return await catalog.attempts.page(tenant,oid,cursor,limit)
