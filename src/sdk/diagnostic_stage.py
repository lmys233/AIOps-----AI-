"""Track a wait phase for registered tasks, without altering scheduling."""
from contextlib import contextmanager
from diagnostics.contract import utc_now
from .diagnostic_state import CURRENT


@contextmanager
def stage(runtime,name,resource_id=None):
    from .cancellation import STATE,OPERATION,REQUESTS,failure_phase
    import asyncio
    state=STATE.get();phase_token=OPERATION.set(name)
    try:root=state is not None and asyncio.current_task() in REQUESTS
    except RuntimeError:root=False
    prior=state['operation'] if root else None
    if root:state['operation']=name
    task_id=CURRENT.get().get('task_id');pool=CURRENT.get().get('worker_pool_id')
    monitor=runtime.diagnostics.workers.get(pool);previous=None;at=utc_now()
    if monitor and task_id in monitor.tasks:
        with monitor.lock:
            previous=monitor.tasks[task_id].get('phase')
            monitor.tasks[task_id]['phase']=name
    try:yield
    except BaseException as error:failure_phase(error,name);raise
    finally:
        OPERATION.reset(phase_token)
        if root:state['operation']=prior
        try:
            if monitor and task_id in monitor.tasks:
                with monitor.lock:monitor.tasks[task_id]['phase']=previous
            runtime.diagnostics.emit('request_phase',dict(phase=name,resource_id=resource_id),'sdk_runtime',
                coverage=dict(start=at,end=utc_now(),complete=True))
        except Exception:runtime.diagnostics.dropped+=1
