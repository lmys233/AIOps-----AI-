"""Bounded side channel. Recording failures never escape into application code."""
from collections import deque
from contextvars import ContextVar
import os
import socket
from threading import RLock
from uuid import uuid4
from diagnostics.contract import event, utc_now
from .redact import redact_event_dict

CURRENT = ContextVar('aiops_diagnostic_context', default={})
CURRENT_RUNTIME = ContextVar('aiops_diagnostic_runtime', default=None)


class DiagnosticState:
    def __init__(self, runtime, capacity=1024):
        self.runtime = runtime
        cfg = runtime.config
        self.identity = dict(tenant=os.getenv('AIOPS_SDK_TENANT','default'),
            env=getattr(cfg,'env',None) or 'unknown', cluster=getattr(cfg,'cluster',None),
            service=runtime.service_name, instance=os.getenv('AIOPS_SDK_INSTANCE') or
            f'{socket.gethostname()}:{os.getpid()}', boot_id=os.getenv('AIOPS_SDK_BOOT_ID') or uuid4().hex)
        self.enabled = os.getenv('AIOPS_SDK_DIAGNOSTICS','on').lower() not in ('0','off','false')
        self.events = deque(maxlen=capacity)
        self.lock = RLock()
        self.capabilities = {}
        self.dropped = 0
        self.routes = set()
        self.holders = {}
        self.workers = {}
        self.sample_counts={};self.sampled=0

    def capability(self, name, status, reason='', adapter=''):
        with self.lock:
            old = self.capabilities.get(name,{})
            self.capabilities[name] = dict(status=status, reason=reason, adapter=adapter,
                last_success=old.get('last_success'), mounted=status=='available')

    def emit(self, kind, payload, source=None, correlation=None, *, force_sample=False, **kwargs):
        if not self.enabled:
            return None
        try:
            from diagnostics.priority import priority
            every=max(1,int(os.getenv('AIOPS_SDK_SUCCESS_SAMPLE_EVERY','10')))
            sampled=not force_sample and priority({'kind':kind,'payload':payload})==0 and every>1
            if sampled:
                self.sample_counts[kind]=self.sample_counts.get(kind,0)+1
                if (self.sample_counts[kind]-1)%every:
                    self.sampled+=1;return None
                kwargs.setdefault('quality',dict(status='partial',reason='Successful observations sampled',
                    sampled=True,truncated=False,dropped=0))
                from diagnostics.contract import utc_now
                at=kwargs.get('occurred_at') or utc_now()
                kwargs['coverage']=dict(kwargs.get('coverage') or {'start':at,'end':at},complete=False)
            clean = redact_event_dict(payload, depth=12)
            if clean.get('truncated'):
                kwargs['quality']=dict(status='partial',reason='Diagnostic payload truncated',sampled=False,truncated=True,dropped=0)
                at=kwargs.get('occurred_at') or utc_now()
                kwargs['coverage']=dict(kwargs.get('coverage') or {'start':at,'end':at},complete=False)
            row = event(self.identity,kind,source or 'sdk_'+kind.split('_')[0],clean,
                        correlation=correlation if correlation is not None else CURRENT.get(),**kwargs)
            with self.lock:
                if len(self.events)==self.events.maxlen:
                    self.dropped += 1
                    from diagnostics.priority import priority
                    if priority(row)==0 and all(priority(r)>0 for r in list(self.events)[:64]):
                        return None
                    if priority(row)>0:
                        index=next((i for i,r in enumerate(self.events) if priority(r)==0),None)
                        if index is not None:del self.events[index]
                self.events.append(row)
                cap = self.capabilities.get(source or 'sdk_'+kind.split('_')[0])
                if cap is not None:cap['last_success']=row['captured_at']
            return row
        except Exception:
            self.dropped += 1
            return None

    def snapshot(self):
        from . import __version__
        with self.lock:
            return dict(identity=dict(self.identity), capabilities={k:dict(v) for k,v in
                self.capabilities.items()}, buffered=len(self.events), dropped=self.dropped,
                captured_at=utc_now(), enabled=self.enabled,sampled_successes=self.sampled,
                sdk_version=__version__,schema_version=2 if 'observed_id' in self.identity else 1,
                limits={'events':self.events.maxlen,'worker_pools':64,'tracked_tasks_per_pool':1024,
                        'routes':1024,'snapshot_threads':32,'snapshot_depth':12,'snapshot_bytes':48000})

    def bounded_route(self, route):
        with self.lock:
            if route in self.routes:return route
            if len(self.routes)>=1024:return '<route-limit>'
            self.routes.add(route)
            return route


def active_runtime():
    from .state import get_runtime
    return CURRENT_RUNTIME.get() or get_runtime()


def current_correlation():
    out = dict(CURRENT.get())
    try:
        from opentelemetry.trace import get_current_span
        ctx = get_current_span().get_span_context()
        if ctx.is_valid:
            out.update(trace_id=f'{ctx.trace_id:032x}',span_id=f'{ctx.span_id:016x}')
            if 'request_id' in CURRENT.get():CURRENT.get().update(out)
    except Exception:
        pass
    return out
