"""Pure ASGI observation: preserve streaming, exceptions and cancellation."""
import asyncio
from time import perf_counter
import traceback
from uuid import uuid4
from prometheus_client import Histogram
from .diagnostic_state import CURRENT, CURRENT_RUNTIME, current_correlation

HTTP_PHASE = Histogram('aiops_http_request_seconds','Full request observation',
    ['service','service_instance_id','method','route','outcome'])


def exception_fields(exc):
    detail = str(exc)
    database_detail=str(getattr(exc,'detail','') or '')
    frames=traceback.extract_tb(exc.__traceback__)
    stack='\n'.join(f'{f.filename.rsplit(chr(92),1)[-1].rsplit("/",1)[-1]}:{f.lineno} in {f.name}' for f in frames)
    return dict(exception_type=type(exc).__name__,sqlstate=getattr(exc,'sqlstate',None),
                message=detail[:12000],stack=stack[:20000],
                detail=database_detail[:20000],truncated=len(detail)>12000 or len(stack)>20000 or len(database_detail)>20000)


class DiagnosticHttpMiddleware:
    def __init__(self, app, runtime):
        self.app,self.runtime=app,runtime
        runtime.diagnostics.capability('sdk_http','available',adapter='asgi')
        runtime.diagnostics.capability('request_disconnect','available',adapter='passive-ASGI-receive')
        runtime.diagnostics.capability('request_cancel','partial','Requires existing framework cancellation result callback','runtime-hook')

    async def __call__(self, scope, receive, send):
        from .contract import PROBE_PATHS
        if not self.runtime.diagnostics.enabled or scope.get('type')!='http' or scope.get('path') in PROBE_PATHS:
            return await self.app(scope,receive,send)
        from .probe_correlation import correlation
        tokens=(CURRENT.set({'request_id':uuid4().hex,**correlation(self.runtime,scope)}),CURRENT_RUNTIME.set(self.runtime))
        started=perf_counter(); state={'status':None,'headers_ms':None,'completed':False}
        from .cancellation import begin,end,disconnect,terminal
        state['scope']=scope;cancellation_token=begin(self.runtime,state)
        async def observe_receive():
            message=await receive()
            if message.get('type')=='http.disconnect':
                try:disconnect(self.runtime,state)
                except Exception:self.runtime.diagnostics.dropped+=1
            return message
        async def observe_send(message):
            current_correlation()
            if message['type']=='http.response.start':
                state.update(status=message['status'],headers_ms=(perf_counter()-started)*1000)
            await send(message)
            if message['type']=='http.response.body' and not message.get('more_body',False):state['completed']=True
        exc=None
        try:
            await self.app(scope,observe_receive,observe_send)
        except BaseException as error:
            exc=error
            raise
        finally:
            try:self.record(scope,state,started,exc)
            except Exception:self.runtime.diagnostics.dropped+=1
            end(cancellation_token);CURRENT.reset(tokens[0]);CURRENT_RUNTIME.reset(tokens[1])

    def record(self, scope, state, started, exc):
        from .middleware_http import route_template,bounded_method
        d=self.runtime.diagnostics
        route=d.bounded_route(route_template(scope,self.runtime))
        method=bounded_method(scope.get('method'))
        outcome=('cancelled' if isinstance(exc,asyncio.CancelledError) else 'exception' if exc
                 else 'server_error' if (state['status'] or 0)>=500 else 'complete')
        if isinstance(exc,asyncio.CancelledError) and state['completed']:outcome='post_response_cancelled'
        elif isinstance(exc,TimeoutError):outcome='post_response_timeout' if state['completed'] else 'timeout'
        elif exc is None and state['status']==504:outcome='timeout'
        payload=dict(route=route,method=method,outcome=outcome,total_ms=(perf_counter()-started)*1000,
                     status=state['status'],headers_ms=state['headers_ms'],completed=state['completed'])
        if exc:payload.update(exception_fields(exc))
        from .cancellation import terminal
        from diagnostics.contract import utc_now
        payload.update(terminal(state,exc));payload['request_started_at']=state['request_started_at']
        corr=current_correlation()
        d.emit('http_request',payload,'sdk_http',corr,force_sample=bool(corr.get('probe_id')),
               coverage=dict(start=state['request_started_at'],end=utc_now(),complete=True))
        HTTP_PHASE.labels(self.runtime.service_name,d.identity['instance'],method,route,outcome).observe(payload['total_ms']/1000)
