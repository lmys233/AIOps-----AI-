"""Bounded stack metadata without locals, source contents, or memory dumps."""
import asyncio
import json
import sys
from time import perf_counter
from itertools import islice
from .diagnostic_state import current_correlation


def _frames(frame,depth,deadline):
    """Frame metadata only: traceback helpers also look up source files."""
    result=[]
    while frame is not None and len(result)<depth:
        if perf_counter()>=deadline:return list(reversed(result)),True
        code=frame.f_code
        result.append(dict(file=code.co_filename.rsplit('\\',1)[-1].rsplit('/',1)[-1],
                           line=frame.f_lineno,function=code.co_name))
        frame=frame.f_back
    return list(reversed(result)),False


def _values(mapping,limit):
    # Copy only the bounded prefix before doing Python work. Holder/worker
    # membership can change in other threads; an interrupted copy is coverage
    # loss rather than failure of the diagnostic endpoint.
    try:return list(islice(mapping.values(),limit)),False
    except RuntimeError:return [],True


def snapshot(runtime,max_threads=32,depth=12,max_ms=10,force_sample=False):
    max_threads=max(1,min(32,max_threads));depth=max(1,min(12,depth))
    d=runtime.diagnostics;started=perf_counter();deadline=started+max(0,max_ms)/1000
    frames=sys._current_frames();limited=len(frames)>max_threads;stacks=[]
    for tid,frame in islice(frames.items(),max_threads):
        if perf_counter()>=deadline:
            limited=True;break
        stack,expired=_frames(frame,depth,deadline);limited=limited or expired
        stacks.append(dict(thread_id=tid,frames=stack))
    async_tasks=[]
    try:
        if perf_counter()<deadline:
            tasks=asyncio.all_tasks()
            limited=limited or len(tasks)>32
            for task in islice(tasks,32):
                if perf_counter()>=deadline:limited=True;break
                async_tasks.append(dict(task_id='async-'+str(id(task)),frames=[dict(function=f.f_code.co_name,
                    line=f.f_lineno) for f in task.get_stack(limit=depth)]))
        else:limited=True
    except RuntimeError:pass
    holders=[]
    holder_values,changed=_values(d.holders,256);limited=limited or changed
    for value in holder_values:
        if perf_counter()>=deadline:limited=True;break
        holders.append(dict(value,hold_ms=(perf_counter()-value['started'])*1000))
    workers=[]
    worker_values,changed=_values(d.workers,64);limited=limited or changed
    for worker in worker_values:
        if perf_counter()>=deadline:limited=True;break
        # A running worker may hold its bookkeeping lock; observation must not wait
        # for that worker or keep the application's event loop blocked.
        lock=getattr(worker,'lock',None)
        if lock is not None and not lock.acquire(blocking=False):limited=True;continue
        try:workers.append(worker.snapshot())
        finally:
            if lock is not None:lock.release()
    payload=dict(stacks=stacks,async_tasks=async_tasks,holders=holders,
                 workers=workers)
    payload['sampling_interval_seconds']=getattr(runtime,'diagnostic_snapshot_interval',0)
    while len(json.dumps(payload,ensure_ascii=False).encode())>48000:
        key=max(('stacks','async_tasks','holders','workers'),key=lambda k:len(json.dumps(payload[k])))
        if not payload[key]:break
        payload[key]=payload[key][:len(payload[key])//2];limited=True
    payload['capture_ms']=(perf_counter()-started)*1000
    limited=limited or perf_counter()>=deadline or len(d.holders)>256 or len(d.workers)>64
    quality=dict(status='partial' if limited else 'available',reason='Snapshot only',sampled=limited,
                 truncated=limited,dropped=d.dropped)
    from diagnostics.contract import utc_now
    now=utc_now()
    return d.emit('runtime_snapshot',payload,'sdk_runtime',current_correlation(),quality=quality,force_sample=force_sample,
                  coverage=dict(start=now,end=now,complete=False))
