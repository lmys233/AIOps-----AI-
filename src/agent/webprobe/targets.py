"""Live PG endpoint authorization; no URL can be supplied by the model."""
from contextvars import ContextVar
from contextlib import contextmanager
import hashlib, json, re, time
from urllib.parse import quote, unquote
from diagnostics.endpoints import inventory, route_path
from ..observed.context import current
from ..observed.sdk_access import target
from ..tools.errors import ToolRefused

CONSENT = ContextVar('active_http_consent', default=None)


def refused(message):
    raise ToolRefused('TOOL_FORBIDDEN', 'ActiveProbeDenied', message)


def fingerprint(params):
    return hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


@contextmanager
def consent(approved):
    token = CONSENT.set(approved)
    try: yield
    finally: CONSENT.reset(token)


async def directory():
    scope = current()
    if not scope: refused('需要选择已登记被观测系统')
    names = await scope.registered()
    rows = await scope.catalog.store.fetch('''SELECT DISTINCT ON(i.service,i.env,coalesce(i.cluster,''),i.base_url) i.*
        FROM agent_observed_instances i JOIN agent_observed_grants g USING(grant_id)
        WHERE i.tenant=$1 AND i.observed_id=$2 AND i.env=$3 AND ($4::text IS NULL OR i.cluster=$4)
        AND i.active AND g.active ORDER BY i.service,i.env,coalesce(i.cluster,''),i.base_url,i.last_registered_at DESC LIMIT 100''',
        scope.tenant, scope.observed_id, scope.env, scope.cluster)
    out=[]
    for row in rows:
        if row['service'] not in names: continue
        out.append(dict(service=row['service'], instance=row['instance'], boot_id=row['boot_id'],
            endpoint_revision=row.get('endpoint_revision',''), discovery=row.get('endpoint_status','legacy_unavailable'),
            endpoints=inventory(row.get('endpoints',[]))))
    return out


def parameters(endpoint, params):
    from jsonschema import Draft202012Validator
    for where, key in [('path', 'path_params'), ('query', 'query')]:
        supplied=params.get(key,{})
        declared={p['name']:p for p in endpoint['parameters'] if p['in']==where}
        # Bare ASGI/Starlette route parameters may lack OpenAPI: unknown args are not guessed.
        if set(supplied)-set(declared): refused('接口参数未登记：'+key)
        for name,p in declared.items():
            if p['required'] and name not in supplied: refused('缺少接口必需参数：'+name)
            if name in supplied and list(Draft202012Validator(p['schema']).iter_errors(supplied[name])):
                refused('接口参数类型不合法：'+name)
    body=params.get('body')
    if body is not None and (endpoint['body_schema'] is None or list(Draft202012Validator(endpoint['body_schema']).iter_errors(body))):
        refused('JSON请求体未登记或类型不合法')
    if endpoint['body_schema'] is not None and body is None and endpoint['body_schema'].get('x-aiops-required',True): refused('该接口需要JSON请求体')
    if len(json.dumps([params.get('query',{}),params.get('path_params',{}),body],allow_nan=False).encode())>8192:
        refused('请求参数超过8KiB')
    def secrets(v):
        if isinstance(v,dict):
            if any(re.search(r'(?i)password|secret|token|authorization|credential',str(k)) for k in v):refused('不能通过模型工具参数提供凭据')
            for i in v.values():secrets(i)
        elif isinstance(v,list):
            for i in v:secrets(i)
    secrets(body);secrets(params.get('query',{}));secrets(params.get('path_params',{}))


async def resolve(params, *, require_consent=True):
    if params.get('target_kind','business')!='business' or params.get('sdk_interface') or params.get('sdk_cluster'):
        refused('业务探测不能使用SDK核验参数')
    scope=current()
    if not scope: refused('需要选择已登记被观测系统')
    row=await target(scope,params['service'],params.get('instance'))
    if row['boot_id']!=params['boot_id']:refused('目标实例已重新启动，请基于新启动记录重新规划')
    if row.get('endpoint_revision')!=params['endpoint_revision']:refused('接口登记版本已变化，请重新选择')
    endpoint=next((e for e in inventory(row.get('endpoints',[])) if e['id']==params['endpoint_id']),None)
    if not endpoint:refused('该业务接口尚未登记')
    policy=endpoint['policy']
    if not policy['probe'] or policy['effect']=='unknown':refused('接口所有者未声明允许探测或副作用')
    if params['mode']=='load' and not policy['load']:refused('接口所有者未允许负载探测')
    for param,key in [('max_requests','max_requests'),('duration_sec','max_duration_sec'),('concurrency','max_concurrency'),('rate_per_sec','max_rps')]:
        if params[param]>policy[key]:refused('超过该接口允许的 '+key)
    if params['mode']=='probe' and (params['max_requests']>2 or params['concurrency']!=1 or params['rate_per_sec']>2):
        refused('低频核验最多2次、单并发、2次/秒；更高负载须显式使用load')
    parameters(endpoint,params)
    if policy['effect']=='write' and require_consent:
        permit=CONSENT.get() or {}
        if permit.get('scope')!=[scope.tenant,scope.observed_id,scope.env,scope.cluster] or permit.get('fingerprint')!=fingerprint(params) or time.time()>permit.get('expires_at',0):
            refused('业务写请求需要针对本次完整参数的单次确认')
    path=endpoint['path']
    for key,value in params.get('path_params',{}).items():
        value=str(value)
        if unquote(value) in ('.','..') or any(s in unquote(value) for s in ('/','\\','?','#')):refused('不合法的路径参数')
        path=re.sub(r'\{'+re.escape(key)+r'(?::[^}]+)?\}',lambda _:quote(value,safe=''),path)
    if '{' in path or '}' in path:refused('路由模板参数未补齐')
    route_path(path)
    from diagnostics.observed import base_url
    origin=base_url(row.get('business_url') or row['base_url'])
    # Revoked origin grants must apply to business targets too.
    grant=await scope.catalog.store.one('SELECT * FROM agent_observed_grants WHERE grant_id=$1 AND active',row['grant_id'])
    if not grant or origin not in grant['origins']:refused('业务地址授权已撤销')
    return row,endpoint,origin+path
