"""Live registration check at the executor boundary, before schema/handler/network."""
from .context import current,enabled


def service_name(value):
    value=str(value or '')
    if value.startswith('svc:'):value=value[4:].split('@',1)[0]
    return value


async def protect(tool,params,definition=None):
    scope=current()
    spec=getattr(params,'query_spec',None)
    if scope is None:
        if enabled():raise PermissionError('choose an authorized observed system before querying')
        if spec:
            from ..queries.compiler import compile_spec
            return compile_spec(spec,params,None,set(),definition)
        return dict(params)
    names=await scope.registered();out=dict(params)
    if tool=='web_query':
        if out.get('target_kind','business')=='sdk':
            from ..webprobe.sdk import resolve
        else:
            from ..webprobe.targets import resolve
        await resolve(out)
    if out.get('service') and service_name(out['service']) not in names:raise PermissionError('service not registered in current system')
    for k,value in [('observed_id',scope.observed_id),('tenant',scope.tenant),('tenant_id',scope.tenant),('env',scope.env)]:
        if k in out and out[k] not in (None,'',value):raise PermissionError('tool scope mismatch: '+k)
    if scope.cluster and out.get('cluster') not in (None,'',scope.cluster):raise PermissionError('cluster mismatch')
    from ..queries.compiler import compile_spec,source_params
    if spec:
        if spec.tool!=tool:raise PermissionError('planned tool differs from execution tool')
        return compile_spec(spec,params,scope,names,definition)
    return source_params(tool,out,scope,names)
