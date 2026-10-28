"""SDK span scope and pre-registration export gate; supports the installed OTel API."""
from .system_scope import identity


class Tagger:
    def __init__(self,runtime):self.runtime=runtime
    def on_start(self,span,parent_context=None):
        i=identity(self.runtime)
        if i:
            for k in ('observed_id','tenant','boot_id'):span.set_attribute('aiops.'+k,i[k])
        else:span.set_attribute('aiops.scope','registration_pending')
    def on_end(self,span):
        i=identity(self.runtime)
        if not i or (span.attributes or {}).get('aiops.observed_id')!=i['observed_id']:return
        ctx=span.context;parent=span.parent
        from datetime import datetime,timezone
        keys={'http.route','http.method','http.status_code','http.request.method','http.response.status_code','db.system','db.operation'}
        attributes={k:v for k,v in (span.attributes or {}).items() if k in keys}
        payload={'name':span.name,'kind':span.kind.name,'start_ns':span.start_time,'end_ns':span.end_time,
            'duration_ms':(span.end_time-span.start_time)/1e6,'status':span.status.status_code.name,
            'attributes':attributes,'span_id':f'{ctx.span_id:016x}',
            'parent_span_id':f'{parent.span_id:016x}' if parent else ''}
        if span.status.status_code.name=='ERROR':payload['outcome']='exception'
        self.runtime.diagnostics.emit('trace_span',payload,'sdk_trace',
            occurred_at=datetime.fromtimestamp(span.end_time/1e9,timezone.utc).isoformat(),
            correlation={'trace_id':f'{ctx.trace_id:032x}','span_id':f'{ctx.span_id:016x}'})
    def shutdown(self):pass
    def force_flush(self,timeout_millis=30000):return True


class Exporter:
    def __init__(self,wrapped,runtime):self.wrapped=wrapped;self.runtime=runtime;self.dropped=0
    def export(self,spans):
        from opentelemetry.sdk.trace.export import SpanExportResult
        i=identity(self.runtime)
        selected=[s for s in spans if i and (s.attributes or {}).get('aiops.observed_id')==i['observed_id']]
        self.dropped+=len(spans)-len(selected)
        if not selected:return SpanExportResult.SUCCESS
        return self.wrapped.export(selected)
    def shutdown(self):return self.wrapped.shutdown()
    def force_flush(self,timeout_millis=30000):
        fn=getattr(self.wrapped,'force_flush',None)
        return fn(timeout_millis) if fn else True


def install(provider,runtime):
    if getattr(provider,'_aiops_scope_installed',False):return True
    processors=getattr(getattr(provider,'_active_span_processor',None),'_span_processors',())
    for p in processors:
        if hasattr(p,'span_exporter'):p.span_exporter=Exporter(p.span_exporter,runtime)
        else:
            runtime.diagnostics.capability('scoped_tracing','unsupported','OTel exporter adapter unsupported')
            # Keep local spans while preventing an unknown processor from exporting unscoped data.
            provider._active_span_processor=Tagger(runtime)
            return False
    provider.add_span_processor(Tagger(runtime));provider._aiops_scope_installed=True
    return True
