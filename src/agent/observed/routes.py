"""Registry endpoints: SDK registration uses enrollment auth, directory uses platform auth."""
import os
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.responses import Response
from .store import RegistryStore
from .credentials import Credentials
from .catalog import Catalog
from .register import register,authenticate
from .enrollment import issue


def build():
    dsn=os.getenv('AIOPS_DATABASE_URL')
    if not dsn:return None
    redis=None
    if os.getenv('AIOPS_REDIS_URL'):
        import redis.asyncio as aioredis
        redis=aioredis.from_url(os.environ['AIOPS_REDIS_URL'],decode_responses=True)
    return Catalog(RegistryStore(dsn),redis)


def install(app,catalog=None,credentials=None):
    catalog=catalog if catalog is not None else build();credentials=credentials or Credentials()
    app.state.observed_catalog=catalog;app.state.observed_credentials=credentials
    if catalog:catalog.credentials=credentials
    from .attempt_routes import install as install_attempts
    install_attempts(app)
    @app.post('/v1/observed/register')
    async def enroll_sdk(request:Request):
        return await guarded(catalog,lambda: register_request(catalog,credentials,request))
    @app.post('/v1/observed-enrollments')
    async def enroll(request:Request):
        return await guarded(catalog,lambda: issue_request(catalog,credentials,request))
    @app.delete('/v1/observed-enrollments/{gid}')
    async def revoke(gid:str):
        return await guarded(catalog,lambda: catalog.revoke(gid))
    @app.get('/v1/observed-systems')
    async def listing(before:str|None=None,limit:int=20):
        return await guarded(catalog,lambda: catalog.page(os.getenv('AIOPS_TENANT','default'),before,limit))
    @app.get('/v1/observed-targets')
    async def prometheus_targets():
        from .discovery import targets
        return await guarded(catalog,lambda: targets(catalog,os.getenv('AIOPS_TENANT','default')))
    @app.get('/v1/observed-systems/{oid}')
    async def detail(oid:str):
        return await guarded(catalog,lambda: detail_body(catalog,oid))
    @app.patch('/v1/observed-systems/{oid}')
    async def active(oid:str,request:Request):
        data=await request.json()
        if set(data)=={'name'}:
            return await guarded(catalog,lambda: catalog.rename(os.getenv('AIOPS_TENANT','default'),oid,data['name']))
        if set(data)-{'active','service'} or type(data.get('active')) is not bool:return JSONResponse({'error':'invalid_state'},422)
        return await guarded(catalog,lambda: catalog.set_active(os.getenv('AIOPS_TENANT','default'),oid,data['active'],data.get('service')))


async def detail_body(catalog,oid):
    tenant=os.getenv('AIOPS_TENANT','default')
    row=await catalog.store.one('SELECT * FROM agent_observed_systems WHERE tenant=$1 AND observed_id=$2',tenant,oid)
    if not row:raise KeyError('system not found')
    row['services']=await catalog.services(tenant,oid)
    return row


def bearer(request):
    value=request.headers.get('authorization','')
    if not value.startswith('Bearer '):raise PermissionError('enrollment credential required')
    return value[7:]


async def register_request(catalog,credentials,request):
    return await register(catalog.store,credentials,bearer(request),await request.json())


async def issue_request(catalog,credentials,request):
    return await issue(catalog.store,credentials,await request.json())


async def registration_auth(app,request,call_next):
    catalog=app.state.observed_catalog
    if catalog is None:return JSONResponse({'error':'registry_unconfigured'},503)
    try:
        await authenticate(catalog.store,app.state.observed_credentials,bearer(request))
        # Authenticate here; parse only after the inner capped-body middleware.
        return await call_next(request)
    except PermissionError:return JSONResponse({'error':'enrollment_unauthorized'},403)
    except (ValueError,KeyError,TypeError):return JSONResponse({'error':'invalid_registration'},422)
    except Exception:return JSONResponse({'error':'registry_unavailable'},503)


async def guarded(catalog,fn):
    if catalog is None:return JSONResponse({'error':'registry_unconfigured'},503)
    try:
        value=await fn()
        if isinstance(value,Response):return value
        return JSONResponse(value if value is not None else {'ok':True})
    except PermissionError:return JSONResponse({'error':'scope_denied'},403)
    except KeyError:return JSONResponse({'error':'not_found'},404)
    except (ValueError,TypeError):return JSONResponse({'error':'invalid_registry_input'},422)
    except Exception as exc:
        import asyncpg
        if isinstance(exc,asyncpg.UniqueViolationError):return JSONResponse({'error':'registry_conflict'},409)
        return JSONResponse({'error':'registry_unavailable'},503)
