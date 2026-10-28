"""One source compilation boundary; live authorization remains outside this module."""
from .selectors import rewrite
from .trace_language import compile_query
from .binding import schema_hash


def source_params(tool,params,scope,names):
    out=dict(params)
    if tool in ('promql_read','logql_read'):
        out['query']=rewrite(out.get('query'),scope,names,logs=tool=='logql_read')
    elif tool=='traceql_read':out['query']=compile_query(out.get('query',''),names)
    return out


def compile_spec(spec,params,scope,names,definition):
    if params!=spec.params():raise ValueError('parameters changed after query was planned')
    if definition is None or definition.version!=spec.tool_version or schema_hash(definition)!=spec.schema_hash:
        raise ValueError('query tool version/schema changed; replan before execution')
    wanted=spec.scope
    if scope is None:
        if wanted.observed_id:raise PermissionError('registered query lost its authorization scope')
        return dict(params)
    if (wanted.tenant,wanted.observed_id,wanted.env,wanted.cluster)!=(scope.tenant,scope.observed_id,scope.env,scope.cluster):
        raise PermissionError('query scope differs from verified execution scope')
    if not wanted.services or not set(wanted.services)<=set(names):raise PermissionError('planned service disabled or unregistered')
    return source_params(spec.tool,params,wanted,set(wanted.services))
