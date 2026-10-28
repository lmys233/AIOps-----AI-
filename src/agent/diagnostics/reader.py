"""Archive readers with target-bound queries and explicit coverage limits."""
import json
from datetime import datetime,timezone
from diagnostics.contract import validate_event
from ..tools.availability import Availability
from ..tools.sources import SourceUnavailable,QueryRejected

KINDS={
    'request_cancellation_read':['http_request','request_disconnect','request_deadline','request_cancel','request_shutdown_cancel'],
    'sdk_capability_read':['capability_report'],
    'runtime_snapshot_read':['request_disconnect','request_deadline','request_cancel','request_shutdown_cancel',
        'runtime_snapshot','capability_report','http_request','downstream_call','sql_error','sql_query',
        'worker_reject','worker_start','worker_finish','request_phase','pool_acquire','pool_release','pool_timeout'],
    'worker_state_read':['runtime_snapshot','worker_reject','worker_start','worker_finish','worker_cancel'],
    'connection_holders_read':['runtime_snapshot','pool_acquire','pool_release','pool_timeout','pool_acquire_failed','sql_query','sql_error'],
    'process_event_read':['process_exit','process_start','process_start_failed','process_state','process_heartbeat','observer_start','sdk_start','sdk_start_failed','sdk_stop','sdk_heartbeat'],
    'db_activity_read':['db_activity','sql_query','sql_error','transaction_event','http_request'],
    'db_lock_read':['db_locks','db_activity'],
    'db_deadlock_read':['db_deadlock','sql_error','sql_query','transaction_event'],
    'db_slow_query_read':['db_slow_queries','sql_query','sql_error'],
    'db_plan_read':['db_plan'],
}
SOURCE_NEEDS={'db_plan_read':{'query_plan'},'db_lock_read':{'database_observer'},
    'request_cancellation_read':{'sdk_http'},
    'sdk_capability_read':{'sdk_runtime'},
    'process_event_read':{'sdk_lifecycle','process_observer','platform_event'}}


class DiagnosticReader:
    def __init__(self,store,tenant='default',bindings=None):
        self.store=store;self.tenant=tenant;self.bindings=bindings

    def availability(self,tool,params):
        from ..observed.context import current
        if current():
            return Availability() if self.store else Availability('unconfigured','诊断档案未接入','配置平台诊断库')
        if self.store is None:return Availability('unconfigured','诊断档案未接入','配置平台诊断库及采集来源')
        if self.bindings is not None and not any(params['service'] in b['services'] and b['tenant']==self.tenant and
            params.get('env')==b['env'] and (not params.get('cluster') or params['cluster']==b.get('cluster')) for b in self.bindings):
            return Availability('unconfigured','目标服务/环境没有受信采集来源','配置 SDK/观察器来源绑定并运行接入自检')
        needs=SOURCE_NEEDS.get(tool)
        if needs and self.bindings is not None and not any(params['service'] in b['services'] and
            params.get('env')==b['env'] and needs.intersection(b['sources']) for b in self.bindings):
            return Availability('unconfigured','该目标没有 '+tool+' 的实际来源','配置相应独立观察器或历史来源，不能以 SDK 错误替代')
        return Availability()

    async def read(self,tool,params):
        if self.store is None:raise SourceUnavailable('diagnostic archive not configured')
        from diagnostics.contract import timestamp
        from ..observed.context import current
        scope=current();extra={'observed_id':scope.observed_id} if scope else {}
        tenant=scope.tenant if scope else self.tenant
        try:
            start,end=(datetime.fromtimestamp(v,timezone.utc) if isinstance(v,(int,float)) else timestamp(v)
                       for v in (params['start'],params['end']))
            if end<start:raise ValueError('inverted window')
        except (ValueError,TypeError):raise QueryRejected('诊断查询需要带时区且顺序正确的时间窗') from None
        try:
            page=await self.store.query(tenant,params['service'],start.isoformat(),end.isoformat(),**extra,
                env=params['env'],cluster=params.get('cluster'),instance=params.get('instance'),
            kinds=KINDS[tool]+['verified_conditions'],limit=params.get('limit',100),before=params.get('before'),request_id=params.get('request_id'))
            # A short old fault must survive a long window of newer heartbeats.
            if not params.get('before') and tool in ('runtime_snapshot_read','db_activity_read','process_event_read'):
                faults=await self.store.query(tenant,params['service'],start.isoformat(),end.isoformat(),**extra,
                    env=params['env'],cluster=params.get('cluster'),instance=params.get('instance'),
                    kinds=KINDS[tool],limit=100,faults_only=True,request_id=params.get('request_id'))
                page['events']=faults['events']+page['events']
                page['fault_page_has_more']=faults.get('has_more',False)
        except Exception as exc:
            raise SourceUnavailable('诊断档案读取失败：'+type(exc).__name__) from None
        rows=[];seen=set()
        for row in page['events']:
            validate_event(row)
            if params.get('object_name') and not matches_object(row,params['object_name']):continue
            key=(row['observation_id'],row['kind'])
            if key not in seen:rows.append(row);seen.add(key)
        from .proofs import derive_rows
        records=[o.model_dump(mode='json') for o in derive_rows(rows)]
        return dict(page,events=rows,records=records)


def matches_object(row,name):
    if row['payload'].get('object_name')==name:return True
    from ..attribution.v2.facts import OBJECT,BARE_OBJECT
    match=OBJECT.search(str(row['payload'].get('message',''))) or BARE_OBJECT.search(str(row['payload'].get('message','')))
    if match and match[1]==name:return True
    return any(r.get('object_name')==name for r in row['payload'].get('records',[]) if isinstance(r,dict))


def summarize(data):
    from diagnostics.priority import priority
    rows=[];size=0;limited=False
    for row in sorted(data.get('events',[]),key=priority,reverse=True):
        encoded=json.dumps(row,ensure_ascii=False)
        if len(rows)>=40 or size+len(encoded)>60000:
            limited=True;continue
        rows.append(row);size+=len(encoded)
    return dict(events=rows,records=data.get('records',[])[:100],
        has_more=data.get('has_more',False),next_cursor=data.get('next_cursor'),
        truncated=limited,requested_window=data.get('requested_window'),complete=data.get('complete',False),
        latest_received_at=data.get('latest_received_at'),retention_policy=data.get('retention_policy'),
        summary=f"取回 {len(rows)} 条诊断观测；历史覆盖须核实"+
                ('；摘要/分页有剩余记录' if limited or data.get('has_more') else ''))
