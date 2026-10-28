"""Pull only signed probe-correlated SDK events, validate and archive before diagnosis."""
import httpx
from diagnostics.contract import validate_event
from ..observed.context import current
from ..observed.sdk_access import target,binding,response_error,SDKAccessError,network_error
from ..observed.credentials import Credentials,pull_token


async def read(params,transport=None):
    scope=current()
    if not scope:raise PermissionError('observed system required')
    row=await target(scope,params['service'],params['instance'])
    if row['boot_id']!=params['boot_id']:raise SDKAccessError('unavailable','探测实例已重启；不能用新启动记录证明旧请求',sent=False)
    identity=binding(row,'probe_observations')
    cap=next((c for c in row['capabilities'] if c['id']=='probe_observations' and c['enabled']),None)
    if not cap:raise SDKAccessError('unavailable','SDK尚未注册主动观测回取接口；需升级SDK',sent=False)
    credentials=getattr(scope.catalog,'credentials',None) or Credentials()
    token=pull_token(credentials.token(row['grant_id']),identity)
    try:
        async with httpx.AsyncClient(timeout=2,follow_redirects=False,trust_env=False,transport=transport) as client:
            async with client.stream('GET',row['base_url']+cap['path'],params={'run_id':params['run_id']},
                headers={'Authorization':'Bearer '+token}) as response:
                if response.status_code!=200:raise response_error(response)
                body=bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body)>524288:raise SDKAccessError('unavailable','主动观测回取超过512KiB')
                import json
                data=json.loads(body)
        if data.get('identity')!=identity:raise SDKAccessError('unavailable','SDK响应身份不符')
        rows=data.get('events',[])
        if not isinstance(rows,list) or len(rows)>100:raise SDKAccessError('unavailable','SDK返回观测超过上限')
        for e in rows:
            validate_event(e)
            if e['identity']!=identity or e['correlation'].get('probe_id')!=params['run_id'] or e['correlation'].get('traffic_origin')!='active_probe':
                raise SDKAccessError('unavailable','SDK观测与主动请求范围不符')
        from ..observed.ingestion import authorize
        grant=await scope.catalog.store.one('SELECT * FROM agent_observed_grants WHERE grant_id=$1 AND active',row['grant_id'])
        if not grant:raise PermissionError('SDK来源授权已撤销')
        for n in range(0,len(rows),32):await authorize(scope.catalog,grant,rows[n:n+32])
        from ..diagnostics.system_store import SystemDiagnosticStore
        if not hasattr(scope.catalog,'probe_archive'):
            scope.catalog.probe_archive=SystemDiagnosticStore(scope.catalog.store.dsn)
            scope.catalog.store.children=getattr(scope.catalog.store,'children',[])+[scope.catalog.probe_archive]
        await scope.catalog.probe_archive.save(rows)
        return dict(events=rows,truncated=bool(data.get('truncated')),complete=False,
            summary=f'已回取并留存{len(rows)}条同run的SDK观测；本地缓冲与采样可能缺少记录',run_id=params['run_id'])
    except httpx.TransportError as exc:raise SDKAccessError(network_error(exc),'主动请求之后SDK观测回取失败',retryable=True) from exc
