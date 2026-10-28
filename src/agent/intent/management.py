"""Platform-authorized routing CRUD; does not use observed SDK enrollment credentials."""
import os
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from .catalog_models import RouteDefinition
from .catalog_store import CatalogConflict, PgIntentStore
from .catalog import IntentCatalog
from .plugin import IntentRoutingPlugin
from .vector_provider import build_provider
from ..common.enums import Intent


class Mutation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: str = Field(min_length=1, max_length=128)
    route: RouteDefinition


def build_plugin():
    dsn = os.getenv('AIOPS_DATABASE_URL')
    store = PgIntentStore(dsn, os.getenv('AIOPS_TENANT', 'default')) if dsn else None
    return IntentRoutingPlugin(IntentCatalog(store), build_provider(),
        float(os.getenv('AIOPS_INTENT_VECTOR_THRESHOLD', '.84')),
        float(os.getenv('AIOPS_INTENT_VECTOR_MARGIN', '.12')))


def install(app, service):
    router = service.router
    plugin = getattr(router, 'routing_plugin', None)
    app.state.intent_plugin = plugin

    async def start():
        if plugin and app.state.owns_router:plugin.schedule_warm()
    app.state.resources.start(start)
    if plugin:
        class WarmWorker:
            async def close(self):
                from ..common.lifecycle import stop_tasks
                await stop_tasks([plugin.warm_task])
        app.state.resources.own('intent warm worker',WarmWorker(),worker=True)
        if app.state.owns_router:
            app.state.resources.own('intent store',plugin.catalog.store)
            app.state.resources.own('intent embedding',plugin.index.provider)

    @app.get('/v1/intents')
    async def listing():
        if not plugin:
            return JSONResponse({'error': 'INTENT_PLUGIN_UNCONFIGURED'}, 503)
        try:
            data, _ = await plugin.catalog.snapshot(strict=True)
            return {**data, 'routing': plugin.status(data['routes']),
                    'handlers': [i.value for i in Intent],
                    'protected': '具体故障处置/明确高危动作守卫与工具权限不可在线关闭'}
        except ConnectionError:
            return JSONResponse({'error': 'INTENT_DATABASE_UNAVAILABLE'}, 503)

    @app.post('/v1/intents')
    async def create(request: Request):
        return await mutation(plugin, 'create', None, request)

    @app.put('/v1/intents/{rid}')
    async def update(rid: str, request: Request):
        return await mutation(plugin, 'update', rid, request)

    @app.delete('/v1/intents/{rid}')
    async def delete(rid: str, revision: str):
        return await mutation(plugin, 'delete', rid, revision=revision)


async def mutation(plugin, action, rid, request=None, revision=None):
    if not plugin:
        return JSONResponse({'error': 'INTENT_PLUGIN_UNCONFIGURED'}, 503)
    try:
        body = Mutation.model_validate(await request.json()) if request else None
        result = await plugin.catalog.mutate(action, rid or body.route.id,
                                             body.route if body else None,
                                             body.revision if body else revision)
        plugin.schedule_warm()
        return {**result, 'vector_update': 'pending', 'routing': plugin.status(result['routes'])}
    except (ValidationError, ValueError) as exc:
        return JSONResponse({'error': 'INVALID_INTENT_CONFIG', 'message': str(exc)[:1500]}, 422)
    except CatalogConflict as exc:
        return JSONResponse({'error': 'INTENT_REVISION_CONFLICT', 'message': str(exc)}, 409)
    except KeyError:
        return JSONResponse({'error': 'INTENT_NOT_FOUND'}, 404)
    except Exception:
        return JSONResponse({'error': 'INTENT_DATABASE_UNAVAILABLE', 'message': '未确认保存成功，请刷新核实'}, 503)
