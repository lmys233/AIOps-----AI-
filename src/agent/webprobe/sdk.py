"""Code-scheduled SDK GET verification, never a model-selected URL or business load."""
import hashlib,json,time
from contextvars import ContextVar
from contextlib import contextmanager
from datetime import datetime,timezone
from ..observed.context import current
from ..tools.errors import ToolRefused

PERMIT=ContextVar('sdk_web_query_permit',default=None)
TOOLS={'snapshot':'sdk_snapshot_read','capabilities':'sdk_interfaces_read','health':'sdk_health_read',
       'probe_observations':'sdk_verification'}


def denied(message):
    error=ToolRefused('TOOL_FORBIDDEN','SDKVerificationDenied',message);error.sent=False
    raise error


def descriptor(row,interface):
    from diagnostics.observed import CAPABILITIES
    if interface not in TOOLS:denied('SDK能力尚无可校验的只读响应适配器')
    cap=next((c for c in row['capabilities'] if c['id']==interface and c['enabled']),None)
    if not cap or cap['method']!='GET' or cap['path']!=CAPABILITIES[interface]:
        denied('失败的SDK接口未登记、已禁用或不符合只读协议')
    value=dict(service=row['service'],instance=row['instance'],boot_id=row['boot_id'],
        target_kind='sdk',sdk_interface=interface,sdk_cluster=row['cluster'] or '',
        endpoint_id='ep_'+hashlib.sha256(('SDK:GET:'+cap['path']).encode()).hexdigest()[:24],
        endpoint_revision=hashlib.sha256(json.dumps(dict(capability=cap,revision=row['revision']),sort_keys=True).encode()).hexdigest())
    return value


def binding(params):
    scope=current()
    if scope is None:denied('SDK核验需要登记系统范围')
    from .targets import fingerprint
    return (scope.tenant,scope.observed_id,scope.env,scope.cluster,fingerprint(params))


@contextmanager
def authorize(params):
    token=PERMIT.set(binding(params))
    try:yield
    finally:PERMIT.reset(token)


def validate(params):
    if PERMIT.get()!=binding(params):denied('SDK核验只允许代码安排的本次精确请求，模型不能自行授权')
    from .models import RequestPlan
    plan=RequestPlan.model_validate({k:v for k,v in params.items() if k!='run_id'})
    if (plan.target_kind!='sdk' or plan.mode!='probe' or plan.max_requests!=1 or plan.concurrency!=1 or
        plan.path_params or plan.body is not None or plan.duration_sec!=1 or
        plan.rate_per_sec!=1 or plan.timeout_sec!=2):denied('SDK核验只允许单次固定只读请求，不接受负载或额外参数')
    import re
    if plan.sdk_interface=='probe_observations':
        if set(plan.query)!={'run_id'} or not re.fullmatch(r'probe_[a-f0-9]{32}',str(plan.query['run_id'])):
            denied('核验该SDK接口需要原请求的有效run_id，不能猜测关联参数')
    elif plan.query:denied('该SDK接口不接受查询参数')
    if plan.sdk_interface not in TOOLS:denied('SDK接口没有可核验的响应适配')
    return plan


async def resolve(params):
    plan=validate(params)
    from ..observed.sdk_access import target
    scope=current();row=await target(scope,plan.service,plan.instance,exact_cluster=plan.sdk_cluster or None)
    selected=descriptor(row,plan.sdk_interface)
    if any(selected[k]!=params[k] for k in ('endpoint_id','endpoint_revision','boot_id','sdk_cluster')):
        denied('SDK目标或登记能力已变化，不能沿用旧核验参数')
    grant=await scope.catalog.store.one('SELECT * FROM agent_observed_grants WHERE grant_id=$1 AND active',row['grant_id'])
    if not grant or row['base_url'] not in grant['origins']:denied('SDK地址授权已撤销')
    return row


async def run(params,transport=None):
    plan=validate(params)
    await resolve(params)
    from ..observed.sdk_access import read
    from diagnostics.observed import CAPABILITIES
    from .runner import receipt
    interface=plan.sdk_interface
    if interface not in TOOLS:denied('SDK接口没有可核验的响应适配')
    at=datetime.now(timezone.utc).isoformat();tick=time.monotonic()
    request_id=hashlib.sha256((params['run_id']+':sdk').encode()).hexdigest()[:32]
    physical=dict(number=1,request_id=request_id,started_at=at,origin='sdk_verification',
        state='in_flight',status=None,error=None)
    await receipt(physical)
    try:
        raw=await read(None,dict(service=plan.service,instance=plan.instance,query=plan.query),TOOLS[interface],
                       expected=params,transport=transport)
        physical.update(status=200,state='completed',delivery_state='response_received')
    except BaseException as exc:
        physical.update(error=getattr(exc,'access_error',type(exc).__name__),
            status=getattr(exc,'http_status',None),state='unknown' if isinstance(exc,__import__('asyncio').CancelledError) else 'failed',
            delivery_state='request_not_sent' if not getattr(exc,'sent',True) else 'response_unverified')
        raise
    finally:
        physical.update(finished_at=datetime.now(timezone.utc).isoformat(),elapsed_ms=round((time.monotonic()-tick)*1000,3))
        await receipt(physical)
    data=raw.data;identity=data['identity'];elapsed=(time.monotonic()-tick)*1000
    return dict(run_id=params['run_id'],origin='sdk_verification',scope=identity,mode='probe',
        endpoint_id=plan.endpoint_id,endpoint_revision=plan.endpoint_revision,method='GET',route=CAPABILITIES[interface],
        started_at=at,finished_at=physical['finished_at'],sent=1,completed=1,errors=0,
        http_status_counts={'200':1},non_success_responses=0,elapsed_ms=elapsed,achieved_qps=None,p95_ms=None,
        results=[physical],sdk_interface=interface,sdk_response=data,raw_ref=raw.raw_ref,
        stop_reason='single_sdk_check',limitations='仅证伪检查时刻当前登记接口仍不可达；不否定历史错误、不证明业务健康')
