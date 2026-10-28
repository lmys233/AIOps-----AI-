"""Observe lifespan messages while forwarding them unchanged."""


async def observe_lifespan(app,runtime,scope,receive,send):
    runtime.diagnostics.capability('sdk_lifecycle','available',adapter='asgi-lifespan')
    async def wrapper(message):
        kinds={'lifespan.startup.complete':'sdk_start','lifespan.startup.failed':'sdk_start_failed',
               'lifespan.shutdown.complete':'sdk_stop','lifespan.shutdown.failed':'sdk_stop_failed'}
        if message.get('type') in kinds:
            runtime.diagnostics.emit(kinds[message['type']],{'message_type':message['type']},'sdk_lifecycle')
        await send(message)
        if message.get('type')=='lifespan.startup.complete':
            from .endpoints import discover
            runtime.endpoint_inventory,runtime.endpoint_status=discover(app,runtime.config)
            from .registration import start
            start(runtime)
        elif message.get('type') in ('lifespan.shutdown.complete','lifespan.shutdown.failed'):
            worker=getattr(runtime,'registrar',None)
            if worker:worker.close()
    await app(scope,receive,wrapper)
