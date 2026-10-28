"""Only delivers archived telemetry; never polls observed applications."""
import asyncio


def install(app):
    async def start():
        store=getattr(app.state,'diagnostic_store',None)
        if store is not None:app.state.telemetry_relay_task=asyncio.create_task(run(store.scoped()))
        catalog=getattr(app.state,'observed_catalog',None)
        if catalog:app.state.attempt_replay_task=asyncio.create_task(replay(catalog))
    app.state.resources.start(start)
    class Workers:
        async def close(self):
            from ..common.lifecycle import stop_tasks
            await stop_tasks([getattr(app.state,'attempt_replay_task',None),
                              getattr(app.state,'telemetry_relay_task',None)])
    owner=app.state.resources
    owner.own('telemetry delivery workers',Workers(),worker=True)
    owner.own('diagnostic ingestion store',lambda:getattr(app.state,'diagnostic_store',None),lazy=True)
    catalog=getattr(app.state,'observed_catalog',None)
    if catalog:
        owner.own('registry store',catalog.store)
        owner.own('registry redis',catalog.redis)
        owner.own('attempt archive',lambda:getattr(catalog,'attempts',None),lazy=True)


async def run(store):
    from .telemetry_outbox import flush
    while True:
        try:await flush(store)
        except asyncio.CancelledError:raise
        except Exception:
            import logging
            logging.getLogger(__name__).warning('Telemetry delivery unavailable; archived outbox retained')
        await asyncio.sleep(1)


async def replay(catalog):
    from .attempts import store_of
    from types import SimpleNamespace
    archive=store_of(SimpleNamespace(catalog=catalog))
    while True:
        try:
            await archive.replay()
            from .access_telemetry import flush
            await flush(archive)
        except asyncio.CancelledError:raise
        except Exception:pass
        await asyncio.sleep(5)
