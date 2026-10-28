"""Observe existing asyncio.Timeout public methods, without adding a timer."""
import asyncio
from datetime import datetime,timezone,timedelta
import functools
from weakref import WeakKeyDictionary
from uuid import uuid4
from .cancellation import STATE,OPERATION,operation as operation_name
from .diagnostic_state import active_runtime,current_correlation
from diagnostics.contract import utc_now

ACTIVE=WeakKeyDictionary()


def payload(item,scope,phase,error=None):
    when=scope.when();loop=asyncio.get_running_loop()
    at=(datetime.now(timezone.utc)+timedelta(seconds=when-loop.time())).isoformat() if when is not None else None
    operation=OPERATION.get();failed=item['state'].get('phase_error')
    if failed and error is not None and error.__cause__ is failed[0]:operation=failed[1]
    return dict(deadline_id=item['deadline_id'],phase=phase,deadline_at=at,configured_at=item['configured_at'],
        expired=scope.expired(),translated_to_timeout=isinstance(error,TimeoutError) and scope.expired(),
        operation=operation_name(operation),request_started_at=item['state']['request_started_at'])


def start_wrapper(original):
    @functools.wraps(original)
    async def enter(scope):
        result=await original(scope);state=STATE.get();runtime=active_runtime()
        if state is not None and runtime and runtime.diagnostics.enabled:
            try:
                if len(ACTIVE)>=1024:runtime.diagnostics.dropped+=1;return result
                item=dict(runtime=runtime,state=state,deadline_id=uuid4().hex,configured_at=utc_now(),correlation=current_correlation())
                ACTIVE[scope]=item
                runtime.diagnostics.emit('request_deadline',payload(item,scope,'configured'),'sdk_runtime',item['correlation'])
            except Exception:runtime.diagnostics.dropped+=1
        return result
    return enter


def exit_wrapper(original):
    @functools.wraps(original)
    async def leave(scope,*args):
        error=None
        try:return await original(scope,*args)
        except BaseException as exc:error=exc;raise
        finally:
            item=ACTIVE.pop(scope,None)
            if item:
                try:
                    row=item['runtime'].diagnostics.emit('request_deadline',payload(item,scope,'expired' if scope.expired() else 'finished',error),
                        'sdk_runtime',item['correlation'])
                    if row and isinstance(error,TimeoutError) and scope.expired():
                        if len(item['state']['deadline_errors'])<8:item['state']['deadline_errors'].append((error,row['event_id']))
                        else:item['runtime'].diagnostics.dropped+=1
                except Exception:item['runtime'].diagnostics.dropped+=1
    return leave


def patch(runtime):
    if not hasattr(asyncio,'Timeout'):
        runtime.diagnostics.capability('request_deadline','unsupported','asyncio.Timeout not available');return
    if not getattr(asyncio.Timeout.__aenter__,'__aiops_deadline__',False):
        enter=start_wrapper(asyncio.Timeout.__aenter__);enter.__aiops_deadline__=True
        asyncio.Timeout.__aenter__=enter;asyncio.Timeout.__aexit__=exit_wrapper(asyncio.Timeout.__aexit__)
    runtime.diagnostics.capability('request_deadline','available',adapter='asyncio.Timeout-public-methods')
