"""Explicit custom-pool protocol and optional standard executor observation."""
from contextvars import copy_context
import functools
from threading import RLock,get_ident
from time import perf_counter
from uuid import uuid4
from weakref import WeakKeyDictionary
from .diagnostic_state import active_runtime,CURRENT,CURRENT_RUNTIME,current_correlation

MONITORS=WeakKeyDictionary()


class WorkerMonitor:
    def __init__(self,runtime,capacity,queue_capacity=None,name=None):
        self.runtime=runtime;self.pool_id=name or uuid4().hex
        self.capacity=capacity;self.queue_capacity=queue_capacity
        self.lock=RLock();self.tasks={};self.active=0;self.queued=0
        self.complete=True;self.rejected=0
        runtime.diagnostics.workers[self.pool_id]=self
        runtime.diagnostics.capability('sdk_worker','available',adapter='registered-worker')

    def submit(self):
        with self.lock:
            if len(self.tasks)>=1024:
                self.complete=False;self.runtime.diagnostics.dropped+=1;return None
            key=uuid4().hex;self.queued+=1
            self.tasks[key]=dict(task_id=key,queued_at=perf_counter(),started=None,
                correlation=current_correlation(),thread_id=None)
            self.emit('worker_submit',key)
            return key

    def start(self,key):
        with self.lock:
            task=self.tasks[key];self.queued-=1;self.active+=1
            task.update(started=perf_counter(),thread_id=get_ident())
            self.emit('worker_start',key)

    def finish(self,key,cancelled=False):
        with self.lock:
            task=self.tasks.get(key)
            if not task:return
            if task['started'] is None:self.queued-=1
            else:self.active-=1
            self.emit('worker_cancel' if cancelled else 'worker_finish',key)
            self.tasks.pop(key,None)

    def reject(self,key,reason='capacity'):
        with self.lock:
            self.rejected+=1
            self.emit('worker_reject',key,reason)
            self.finish(key,True)

    def snapshot(self):
        with self.lock:
            return dict(pool_id=self.pool_id,capacity=self.capacity,queue_capacity=self.queue_capacity,
                active=self.active,queued=self.queued,rejected=self.rejected,complete=self.complete and len(self.tasks)<=64,
                tasks=[dict(t) for t in self.tasks.values()][:64])

    def emit(self,kind,key,reason=''):
        try:
            task=self.tasks.get(key,{})
            payload=dict(pool_id=self.pool_id,capacity=self.capacity,queue_capacity=self.queue_capacity,
                active=self.active,queued=self.queued,reason=reason,
                wait_ms=(task.get('started') or perf_counter())*1000-task.get('queued_at',perf_counter())*1000,
                run_ms=(perf_counter()-task['started'])*1000 if task.get('started') else None)
            self.runtime.diagnostics.emit(kind,payload,'sdk_worker',
                dict(task.get('correlation',{}),task_id=key) if key else task.get('correlation',{}))
        except Exception:self.runtime.diagnostics.dropped+=1


def register_worker(runtime,capacity,queue_capacity=None,name=None):
    if len(runtime.diagnostics.workers)>=64:raise ValueError('worker pool tracking limit')
    return WorkerMonitor(runtime,capacity,queue_capacity,name)


def submit_wrapper(original):
    @functools.wraps(original)
    def submit(executor,fn,*args,**kwargs):
        monitor=MONITORS.get(executor)
        if monitor is None:return original(executor,fn,*args,**kwargs)
        try:key=monitor.submit()
        except Exception:
            monitor.runtime.diagnostics.dropped+=1
            return original(executor,fn,*args,**kwargs)
        if key is None:return original(executor,fn,*args,**kwargs)
        ctx=copy_context()
        def invoke():
            from .state import safe_record
            safe_record(monitor.runtime,'worker_start','worker start',lambda:monitor.start(key))
            token=CURRENT.set(dict(CURRENT.get(),task_id=key,worker_pool_id=monitor.pool_id));rt=CURRENT_RUNTIME.set(monitor.runtime)
            try:return fn(*args,**kwargs)
            finally:
                safe_record(monitor.runtime,'worker_finish','worker finish',lambda:monitor.finish(key))
                CURRENT.reset(token);CURRENT_RUNTIME.reset(rt)
        try:future=original(executor,lambda:ctx.run(invoke))
        except BaseException:
            from .state import safe_record
            safe_record(monitor.runtime,'worker_reject','worker reject',lambda:monitor.reject(key,'submission_failed'))
            raise
        from .state import safe_record
        future.add_done_callback(lambda f:safe_record(monitor.runtime,'worker_finish','worker finish',
                                                     lambda:monitor.finish(key,f.cancelled())))
        return future
    return submit


def patch(runtime):
    from concurrent.futures import ThreadPoolExecutor
    original=ThreadPoolExecutor.__init__
    if not getattr(original,'__aiops_worker__',False):
        @functools.wraps(original)
        def init(executor,*args,**kwargs):
            original(executor,*args,**kwargs)
            active=active_runtime()
            if active is None:return
            try:MONITORS[executor]=register_worker(active,executor._max_workers)
            except Exception:active.diagnostics.capability('sdk_worker','partial','Pool tracking unavailable')
        init.__aiops_worker__=True;ThreadPoolExecutor.__init__=init
        ThreadPoolExecutor.submit=submit_wrapper(ThreadPoolExecutor.submit)
    runtime.diagnostics.capability('sdk_worker','partial','Executor factory patched; awaiting actual worker')
