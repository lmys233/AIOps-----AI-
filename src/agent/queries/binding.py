"""Existing source planners translated once into immutable query specifications."""
import hashlib,json,os
from datetime import datetime,timezone
from ..domain.queries import QueryScope,QuerySpec,QueryWindow
from .capabilities import source_of


def enabled():return os.getenv('AIOPS_QUERY_SPEC','on').lower() not in ('off','false','0')


def scope_of(observed,services=()):
    if not observed:return None
    names=tuple(services) or tuple(observed.names)
    if not set(names)<=set(observed.names):raise PermissionError('query targets exceed verified registration scope')
    return QueryScope(tenant=observed.tenant,observed_id=observed.observed_id,
        env=observed.env,cluster=observed.cluster,services=names)


def instant(value):
    if isinstance(value,(float,int)):return datetime.fromtimestamp(value,timezone.utc)
    if isinstance(value,datetime):return value
    return datetime.fromisoformat(str(value).replace('Z','+00:00'))


def window(params):
    if 'start' not in params and 'end' not in params:return None
    if not params.get('start') or not params.get('end'):raise ValueError('query start/end must be supplied together')
    return QueryWindow(start=instant(params['start']),end=instant(params['end']))


def schema_hash(definition):
    return hashlib.sha256(json.dumps(definition.spec.input_schema,sort_keys=True).encode()).hexdigest()[:16]


def bind(scope,tool,params,definition,*,signal='',purpose='evidence',requested=None):
    if not enabled():return None
    effective=window(params);source=source_of(definition) or 'reference'
    coverage='sampled_window' if effective else 'reference' if source=='reference' else 'current_snapshot'
    if 'raw_ref' in params or 'trace_id' in params:coverage='reference'
    if source=='metrics' and signal.startswith('window_'):coverage='window_aggregate'
    return QuerySpec(scope=scope,tool=tool,source=source,signal=signal,purpose=purpose,
        requested_window=requested,effective_window=effective,coverage=coverage,
        params_json=json.dumps(params,sort_keys=True,ensure_ascii=False,allow_nan=False),
        tool_version=getattr(definition,'version',1),schema_hash=schema_hash(definition))


def for_request(ctx,request,definition,params=None):
    scope=ctx.query_scope or QueryScope(tenant=ctx.tenant_id,env=ctx.slots.env or 'prod',
        cluster=ctx.slots.cluster or None,services=tuple(ctx.services))
    requested=QueryWindow(start=ctx.window_start,end=ctx.window_end)
    spec=bind(scope,request.tool,request.params if params is None else params,definition,
        signal=request.signal,purpose=request.purpose,requested=requested)
    if spec and scope.observed_id:
        from .compiler import compile_spec
        compile_spec(spec,spec.params(),scope,set(scope.services),definition)
    return spec


class QueryParameters(dict):
    def __init__(self,spec):super().__init__(spec.params());self.query_spec=spec
