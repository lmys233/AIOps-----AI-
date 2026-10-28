"""Authenticated catalog/lifecycle plus a bounded, non-diagnostic input preview."""
import asyncio
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel,ConfigDict,Field,field_validator,StrictInt
from .service import engine_of
from .parsing import parse
from .execution import run
from ..observed.context import use
from ..observed.routes import guarded
from ..alerts.routes import scope_for
from ..orchestration.budget import Budget
from ..llm.accounting import collect_records,summary


class Preview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    message:str=Field(min_length=1,max_length=4000)
    env:str='prod'
    cluster:str|None=None
    timezone:str=Field(default='Asia/Shanghai',max_length=64)
    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls,value):
        from ..api.requests import ChatContext
        return ChatContext(timezone=value).timezone


class Suggest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    q:str=Field(default='',max_length=256)
    env:str='prod'
    cluster:str|None=None
    limit:StrictInt=Field(default=20,ge=1,le=20)


def install(app,service):
    @app.post('/v1/observed-systems/{oid}/commands/suggest')
    async def suggest(oid:str,payload:Suggest):
        async def read():
            scope=await scope_for(service,oid,payload.env,payload.cluster);engine=engine_of(service)
            # Check live authorization and service membership even on cache hits.
            if not await engine.enabled('warn'):raise ValueError('warn命令已禁用')
            names=await scope.registered();snapshot=await engine.warnings.snapshot(scope)
            try:return await engine.suggestions.read(scope,payload.q.strip(),payload.limit,names,snapshot)
            except TimeoutError:return JSONResponse({'error':'名称/ID候选查询超时；没有把失败当作空集合'},504)
            except RuntimeError:return JSONResponse({'error':'候选查询繁忙，请稍后重试'},503)
        try:
            async with asyncio.timeout(4):return await guarded(service.observed_catalog,read)
        except TimeoutError:return JSONResponse({'error':'名称/ID候选查询超时；没有把失败当作空集合'},504)
        except RuntimeError:return JSONResponse({'error':'候选查询繁忙，请稍后重试'},503)

    @app.get('/v1/commands')
    async def catalog():
        try:return dict(commands=await engine_of(service).sync())
        except Exception:return JSONResponse({'error':'command_catalog_unavailable'},503)

    @app.post('/v1/commands/{name}')
    async def register(name:str):
        try:return await engine_of(service).set(name,True)
        except ValueError as exc:return JSONResponse({'error':str(exc)},422)
        except Exception:return JSONResponse({'error':'command_registry_unavailable'},503)

    @app.delete('/v1/commands/{name}')
    async def destroy(name:str):
        try:return await engine_of(service).set(name,False)
        except ValueError as exc:return JSONResponse({'error':str(exc)},422)
        except Exception:return JSONResponse({'error':'command_registry_unavailable'},503)

    @app.post('/v1/observed-systems/{oid}/commands/preview')
    async def preview(oid:str,payload:Preview):
        async def read():
            scope=await scope_for(service,oid,payload.env,payload.cluster)
            if not (await service.observed_catalog.store.one('SELECT active FROM agent_observed_systems WHERE tenant=$1 AND observed_id=$2',scope.tenant,oid))['active']:
                raise PermissionError('系统已禁用')
            # Preview can read warning claims; it never diagnoses or opens a chat.
            engine=engine_of(service);chain=parse(payload.message);catalog=await engine.sync()
            for step in chain:
                if not any(c['name']==step['command'] for c in catalog):raise ValueError('命令未注册或已销毁：'+step['command'])
            if len(chain)==1 and chain[0]['command']=='select':
                return dict(help='按时间、位置和内容筛选上步结果',items=[],total=0)
            with use(scope):
                if len(chain)==1 and chain[0]['command']=='warn' and not chain[0]['argument']:
                    from .builtins import bind
                    snapshot=await engine.warnings.snapshot(scope)
                    with bind(engine,None,snapshot):result=await engine.registry.use('warn',dict(where={},limit=20,input_ref='preview:'+str(snapshot)))
                    if not result.ok:return JSONResponse({'error':result.summary},503)
                    return dict(result.data['records'][0],preview=True,model_usage=summary([]))
                # Reject excess preview model traffic before waiting on the semaphore.
                from ..tools.rate_limit import RateLimiter
                if not hasattr(engine,'preview_rate'):engine.preview_rate=RateLimiter()
                allowed=engine.preview_rate.check('preview:'+scope.namespace,'commands','12/min')
                if not allowed.allowed:return JSONResponse({'error':'输入提示请求过频，请稍后重试'},429)
                async with engine.preview_lock:
                    with collect_records() as records:
                        try:outcome=await run(engine,payload.message,Budget(max_llm_calls=12,max_tokens=24000,max_tool_calls=16,max_wall_time_sec=60),timezone_name=payload.timezone,preview=True)
                        except Exception:return JSONResponse({'error':'命令提示未完成；未猜测命中结果','model_usage':summary(records)},502)
                    if outcome['action'] in ('failed','clarify'):
                        return dict(items=[],total=0,question=outcome['question'],preview=True,model_usage=summary(records))
                    return dict(outcome['result'],preview=True,model_usage=summary(records))
        with collect_records() as all_records:
            try:
                # Includes waiting for the shared preview semaphore; queued typing
                # requests do not receive a fresh unlimited HTTP deadline.
                async with asyncio.timeout(60):return await guarded(service.observed_catalog,read)
            except TimeoutError:
                return JSONResponse({'error':'输入提示达到60秒总期限；未猜测匹配结果','model_usage':summary(all_records)},504)
