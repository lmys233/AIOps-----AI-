"""One logical call, separate attempt witnesses, and a terminal result; no model payload duplication."""
import asyncio,json,math,os,re,time,uuid
from contextvars import ContextVar
from datetime import datetime,timezone
from .context import current
from .attempt_store import AttemptStore

ACTIVE=ContextVar('tool_attempt_context',default=None)
SECRET=re.compile(r'(password|passwd|authorization|credential|secret|token|dsn|connection_string)',re.I)


def safe(value,depth=0):
    if depth>12:return {'truncated':'depth_limit'}
    if isinstance(value,dict):return {str(k):'[redacted]' if SECRET.search(str(k)) else safe(v,depth+1) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [safe(v,depth+1) for v in value[:20000]]
    if isinstance(value,str):
        value=re.sub(r'(?i)(Bearer\s+|enr_)[A-Za-z0-9_.-]+','[redacted]',value)
        return re.sub(r'\b(?:postgres(?:ql)?|redis)://\S+','[redacted-dsn]',value)
    if isinstance(value,float) and not math.isfinite(value):
        return {'unavailable_number':'NaN' if math.isnan(value) else '+Infinity' if value>0 else '-Infinity'}
    if value is None or isinstance(value,(int,float,bool)):return value
    return str(value)


def bounded(value,limit=60000):
    value=safe(value);encoded=json.dumps(value,ensure_ascii=False,default=str)
    if len(encoded.encode())<=limit:return value
    return {'truncated':True,'original_bytes':len(encoded.encode()),'preview':encoded[:limit//4]}


def store_of(scope):
    if not hasattr(scope.catalog,'attempts'):
        scope.catalog.attempts=AttemptStore(scope.catalog.store.dsn)
        scope.catalog.store.children=getattr(scope.catalog.store,'children',[])+[scope.catalog.attempts]
    return scope.catalog.attempts


def classify(exc,sent=True):
    if getattr(exc,'access_error',None):return exc.access_error
    if isinstance(exc,asyncio.TimeoutError):return 'read_timeout'
    if isinstance(exc,asyncio.CancelledError):return 'cancelled'
    if exc is None:return 'none'
    return 'unavailable' if not sent or hasattr(exc,'error_type') else 'internal_error'


class Call:
    def __init__(self,executor,tool,params):
        self.scope=current();self.tool=tool;self.params=params;self.count=0;self.rows=[]
        self.cid='call_'+uuid.uuid4().hex;self.iid=executor.context.incident_id or executor.incident_id or ''
        known=getattr(self.scope,'incident_ids',set())
        if not self.iid and len(known)==1:self.iid=next(iter(known))
        self.target={};self.transport={'promql_read':'metrics','logql_read':'logs','traceql_read':'traces'}.get(tool,'archive')
        self.query_spec=getattr(params,'query_spec',None)

    async def record(self,start,elapsed,*,sent,error,result,timeout=0,original=None):
        self.count+=1;s=self.scope
        identity={'tenant':s.tenant,'observed_id':s.observed_id,'service':self.params.get('service') or '_system',
            'env':s.env,'cluster':s.cluster, 'instance':'_aggregate','boot_id':'_aggregate'}
        identity.update(self.target)
        aid=f'att_{time.time_ns():020d}_{uuid.uuid4().hex}'
        row={'schema_version':1,'call_id':self.cid,'attempt_id':aid,'incident_id':self.iid,
            'observer_id':os.getenv('AIOPS_OBSERVER_ID','aiops-local'),'identity':identity,'tool':self.tool,
            'interface_id':getattr(self,'interface',''),'transport':self.transport,'attempt':self.count,
            'sent':sent,'started_at':start,'finished_at':datetime.now(timezone.utc).isoformat(),
            'elapsed_ms':elapsed,'timeout_sec':timeout,'params':bounded(dict(self.params)),
            'result':bounded(result),'ok':error=='none','error':error,
            'http_status':getattr(self,'http_status',None),'raw_ref':f'attempt:{s.observed_id}:{aid}'}
        original=dict(original or {})
        if self.query_spec:original['query_spec']=self.query_spec.summary()
        try:state=await store_of(s).save(row,bounded(original,4000000))
        except Exception:state='archive_failed'
        from .access_status import remember
        remember(s,row,state)
        self.rows.append(row);s.attempts.append({'attempt_id':aid,'call_id':self.cid,'tool':self.tool,
            'error':error,'sent':sent,'transport':self.transport,'persist_state':state,'raw_ref':row['raw_ref'],
            **({'query_spec':self.query_spec.summary()} if self.query_spec else {})})

    async def invoke(self,definition,context,params,timeout):
        from ..tools.handlers import invoke_handler
        self.params=params
        start=datetime.now(timezone.utc).isoformat();tick=time.monotonic();error='none';payload={};original={};sent=True
        self.http_status=None
        try:
            raw=await asyncio.wait_for(invoke_handler(definition.handler,context,params),timeout)
            original={'data':raw.data,'note':raw.note,'raw_ref':raw.raw_ref};payload=original
            return raw
        except BaseException as exc:
            sent=getattr(exc,'sent',True)
            if getattr(exc,'http_status',None):self.http_status=exc.http_status
            error=classify(exc);payload={'error_type':type(exc).__name__,'message':str(exc)};raise
        finally:
            if error=='read_timeout' and timeout<definition.spec.timeout_sec:error='budget_stop'
            if self.scope:await self.record(start,(time.monotonic()-tick)*1000,sent=sent,error=error,result=payload,timeout=timeout,original=original)


def observed_call(implementation):
    from functools import wraps
    @wraps(implementation)
    async def wrapped(executor,role,tool,params):
        return await call(executor,role,tool,params,implementation)
    return wrapped


async def call(executor,role,tool,params,implementation):
    scope=current()
    if not scope:return await implementation(executor,role,tool,params)
    ctx=Call(executor,tool,params);token=ACTIVE.set(ctx);start=datetime.now(timezone.utc).isoformat()
    try:
        result=await implementation(executor,role,tool,params)
        if getattr(ctx,'replayed',False):return result
        if not ctx.count or result.error_code=='BUDGET_EXHAUSTED':
            error='budget_stop' if result.error_code=='BUDGET_EXHAUSTED' else 'invalid_params' if result.error_code=='TOOL_INVALID_PARAMS' else 'policy_denied' if result.error_code=='TOOL_FORBIDDEN' else 'unavailable'
            await ctx.record(start,result.elapsed_ms,sent=False,error=error,result=result.model_dump(mode='json'))
        try:
            state=await store_of(scope).finish(scope.tenant,scope.observed_id,ctx.cid,ctx.iid,bounded(result.model_dump(mode='json')))
            if state!='persisted':scope.attempts.append({'call_id':ctx.cid,'persist_state':'terminal_'+state})
        except Exception:scope.attempts.append({'call_id':ctx.cid,'persist_state':'terminal_archive_failed'})
        return result
    except asyncio.CancelledError:
        if not ctx.count:await ctx.record(start,0,sent=False,error='cancelled',result={'status':'cancelled'})
        try:
            state=await store_of(scope).finish(scope.tenant,scope.observed_id,ctx.cid,ctx.iid,{'ok':False,'status':'cancelled','error_type':'CancelledError'})
            if state!='persisted':scope.attempts.append({'call_id':ctx.cid,'persist_state':'terminal_'+state})
        except Exception:scope.attempts.append({'call_id':ctx.cid,'persist_state':'terminal_archive_failed'})
        raise
    finally:ACTIVE.reset(token)


async def invoke(definition,context,params,timeout):
    ctx=ACTIVE.get()
    if ctx:return await ctx.invoke(definition,context,params,timeout)
    from ..tools.handlers import invoke_handler
    return await asyncio.wait_for(invoke_handler(definition.handler,context,params),timeout)
