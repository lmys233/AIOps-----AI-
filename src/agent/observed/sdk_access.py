"""Fixed registered SDK interfaces. No caller URL, redirect, heartbeat or cross-system fallback."""
import asyncio,json,socket,time
import math
from .context import current
from .attempts import ACTIVE
from .credentials import Credentials,pull_token
from ..tools.errors import SourceUnavailable

INTERFACES={'sdk_snapshot_read':'snapshot','sdk_interfaces_read':'capabilities','sdk_health_read':'health'}
BREAKERS={}
UNSET_CLUSTER=object()


class SDKAccessError(SourceUnavailable):
    def __init__(self,error,message,status=None,retryable=False,retry_after=.2,sent=True):
        super().__init__('SDK_'+error,message);self.access_error=error;self.http_status=status
        self.retry_after=max(retry_after,.05)
        self.retryable=retryable and self.retry_after<=1;self.sent=sent


async def target(scope,service,instance=None,*,exact_cluster=UNSET_CLUSTER):
    if service not in await scope.registered():raise PermissionError('service not registered')
    row=await scope.catalog.store.one('''SELECT i.* FROM agent_observed_instances i
        JOIN agent_observed_grants g USING(grant_id)
        WHERE i.tenant=$1 AND i.observed_id=$2 AND i.service=$3 AND i.env=$4
        AND ($5::text IS NULL OR i.cluster=$5) AND ($6::text IS NULL OR i.instance=$6)
        AND (NOT $7::boolean OR i.cluster IS NOT DISTINCT FROM $8::text)
        AND NOT EXISTS(SELECT 1 FROM agent_observed_instances newer JOIN agent_observed_grants ng USING(grant_id)
            WHERE newer.tenant=i.tenant AND newer.observed_id=i.observed_id AND newer.service=i.service
            AND newer.env=i.env AND newer.cluster IS NOT DISTINCT FROM i.cluster AND newer.base_url=i.base_url
            AND newer.active AND ng.active AND newer.last_registered_at>i.last_registered_at)
        AND i.active AND g.active ORDER BY i.last_registered_at DESC LIMIT 1''',
        scope.tenant,scope.observed_id,service,scope.env,scope.cluster,instance,exact_cluster is not UNSET_CLUSTER,
        None if exact_cluster is UNSET_CLUSTER else exact_cluster)
    if not row:raise SDKAccessError('unavailable','没有可访问的已登记实例',sent=False)
    return row


def binding(row,interface):
    ctx=ACTIVE.get()
    identity={k:row[k] for k in ('tenant','observed_id','service','env','cluster','instance','boot_id')}
    if ctx:ctx.transport='sdk_direct';ctx.target=identity;ctx.interface=interface
    return identity


def network_error(exc):
    import httpx
    if isinstance(exc,httpx.ConnectTimeout):return 'connect_timeout'
    if isinstance(exc,httpx.ReadTimeout):return 'read_timeout'
    cause=exc
    while getattr(cause,'__cause__',None):cause=cause.__cause__
    if isinstance(cause,socket.gaierror):return 'dns_error'
    text=str(exc).lower()
    if isinstance(exc,httpx.ConnectError) and any(t in text for t in ('10061','refused','拒绝')):return 'connection_refused'
    return 'connection_error'


def response_error(response):
    status=response.status_code
    error={401:'unauthorized',403:'forbidden',404:'not_found',429:'rate_limited'}.get(status,'http_5xx' if status>=500 else 'invalid_params')
    hint=response.headers.get('retry-after')
    try:delay=float(hint) if hint is not None else .2
    except ValueError:
        from datetime import datetime,timezone
        from email.utils import parsedate_to_datetime
        try:
            at=parsedate_to_datetime(hint)
            if at.tzinfo is None:at=at.replace(tzinfo=timezone.utc)
            delay=(at-datetime.now(timezone.utc)).total_seconds()
        except (ValueError,TypeError,OverflowError):delay=2
    if not math.isfinite(delay) or delay<0:delay=2
    delay=max(.05,delay)
    # The executor permits one short retry. A longer server cooldown must stay
    # a rate-limit result instead of being silently shortened into another 429.
    retryable=(status>=500 or status==429) and delay<=1
    message=f'SDK 接口返回 HTTP {status}'
    if not retryable and (status==429 or status>=500):message+=f'；服务端要求至少等待 {delay:g}s，本次停止自动重试'
    return SDKAccessError(error,message,status,retryable,delay)


async def read(ctx,params,tool,*,expected=None,transport=None):
    from ..tools.sources import RawOutcome
    import httpx
    scope=current()
    if not scope:raise SDKAccessError('policy_denied','需要选择已登记系统',sent=False)
    row=await target(scope,params['service'],params.get('instance'),exact_cluster=(expected.get('sdk_cluster') or None) if expected else UNSET_CLUSTER)
    interface=INTERFACES.get(tool) or (expected or {}).get('sdk_interface')
    if not interface:raise SDKAccessError('unavailable','未适配的SDK接口',sent=False)
    if expected is not None:
        from ..webprobe.sdk import descriptor
        selected=descriptor(row,interface)
        if row['boot_id']!=expected['boot_id'] or selected['endpoint_id']!=expected['endpoint_id'] or selected['endpoint_revision']!=expected['endpoint_revision']:
            raise SDKAccessError('policy_denied','SDK登记目标或能力已变化，需要重新核验',sent=False)
    identity=binding(row,interface);cap=next((c for c in row['capabilities'] if c['id']==interface and c['enabled']),None)
    if not cap:raise SDKAccessError('unavailable','该实例未登记所需 SDK 接口能力',sent=False)
    key=(scope.tenant,scope.observed_id,row['service'],row['instance'],row['boot_id'],interface)
    failures,until=BREAKERS.get(key,(0,0))
    if time.monotonic()<until and expected is None:raise SDKAccessError('unavailable','该接口近期访问连续失败；按需熔断，稍后提问可再次核查',sent=False)
    credentials=getattr(scope.catalog,'credentials',None) or Credentials()
    token=pull_token(credentials.token(row['grant_id']),identity)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(2),follow_redirects=False,trust_env=False,transport=transport) as client:
            response=await client.get(row['base_url']+cap['path'],params=params.get('query') if expected else None,
                                     headers={'Authorization':'Bearer '+token})
        if ACTIVE.get():ACTIVE.get().http_status=response.status_code
        if response.status_code!=200:raise response_error(response)
        if len(response.content)>65536:raise SDKAccessError('unavailable','SDK 响应超出上限')
        try:data=response.json()
        except ValueError:raise SDKAccessError('unavailable','SDK 响应不是受支持的 JSON')
        if not isinstance(data,dict) or data.get('identity')!=identity:raise SDKAccessError('unavailable','SDK 响应身份与登记实例不一致')
        BREAKERS.pop(key,None)
        return RawOutcome(data=data,raw_ref='sdk:'+scope.observed_id+':'+row['service']+':'+interface,note='实际按需访问；仅证明本次接口响应')
    except httpx.TransportError as exc:
        error=SDKAccessError(network_error(exc),'从 AIOps 到已登记 SDK 接口的网络访问失败',retryable=True)
        BREAKERS[key]=(failures+1,time.monotonic()+30 if failures>=3 else 0);raise error from exc
    except SDKAccessError as exc:
        if exc.retryable:BREAKERS[key]=(failures+1,time.monotonic()+30 if failures>=3 else 0)
        raise


def register_tools(registry):
    from ..tools.definition import define_tool
    from ..tools.policy import CAP_RUNTIME
    for name in INTERFACES:
        async def handler(ctx,params,name=name):return await read(ctx,params,name)
        definition=define_tool(name=name,description='按需访问当前系统已登记 SDK 只读接口；失败不代表业务宕机',
            parameters={'type':'object','required':['service'],'additionalProperties':False,
                'properties':{'service':{'type':'string'},'instance':{'type':'string'}}},
            capabilities=(CAP_RUNTIME,),granted_roles=('runtime_agent',),timeout_sec=3)(handler)
        registry.register(definition,actor='system',reason='registered SDK access')
