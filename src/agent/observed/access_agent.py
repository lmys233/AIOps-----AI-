"""Reads saved access observations, never probes SDKs or treats repeat reads as new witnesses."""
from datetime import datetime,timezone
from diagnostics.contract import event,timestamp
from .context import current
from .attempts import store_of
from ..agents.base import ForensicsAgent,ToolRequest,target_services
from ..common.enums import EvidenceSource
from ..diagnostics.evidence import from_event


def as_event(row):
    payload={k:row[k] for k in ('error','ok','sent','elapsed_ms','http_status','tool','interface_id','observer_id','call_id','attempt','attempt_id')}
    payload['endpoint']=row['params'].get('endpoint','')
    if row['interface_id']=='probe_observations':
        query=row['params'].get('query') or ({'run_id':row['params']['run_id']} if row['params'].get('run_id') else {})
        if query:payload['sdk_query']=query
    e=event(row['identity'],'sdk_access','aiops_sdk_access',payload,occurred_at=row['finished_at'],
        observation_id=row['call_id'],raw_ref=row['raw_ref'])
    e['event_id']=row['attempt_id'];e['captured_at']=row['finished_at']
    return e


async def read(ctx,params):
    from ..tools.sources import RawOutcome,QueryRejected
    scope=current()
    if not scope:raise QueryRejected('SDK 访问档案需要选择系统')
    if params['service'] not in await scope.registered():raise QueryRejected('服务未登记')
    archive=store_of(scope)
    start,end=(datetime.fromtimestamp(v,timezone.utc) if isinstance(v,(int,float)) else timestamp(v) for v in (params['start'],params['end']))
    page=await archive.page(scope.tenant,scope.observed_id,params.get('before'),params.get('limit',50),params['service'],start,end,transport='sdk_direct',sent=True)
    rows=[r for r in page['attempts'] if r['transport']=='sdk_direct' and r['sent']]
    return RawOutcome(data={'events':[as_event(r) for r in rows], 'has_more':page['has_more'],
        'next_cursor':page['next_cursor'],'coverage':'bounded saved SDK access attempts, not current liveness'},
        raw_ref='sdk_access_archive:'+scope.observed_id+':'+params['service'])


def summarize(data):
    return dict(data,summary=f"{len(data['events'])} 条留存 SDK 访问记录；不能证明当前状态",truncated=data['has_more'])


def register_tools(registry):
    from ..tools.definition import define_tool
    from ..tools.summarize import SUMMARIZERS
    from ..tools.policy import CAP_SDK_ACCESS,CAP_RAW_TELEMETRY
    SUMMARIZERS['summarize_sdk_access']=summarize
    schema={'type':'object','required':['service','start','end'],'additionalProperties':False,
        'properties':{'service':{'type':'string'},'start':{'type':'string'},'end':{'type':'string'},
            'limit':{'type':'integer','minimum':1,'maximum':50},'before':{'type':'string'}}}
    d=define_tool(name='sdk_access_read',description='只读当前系统已登记服务的 SDK 访问档案，不发起保活探测',
        parameters=schema,capabilities=(CAP_RAW_TELEMETRY,CAP_SDK_ACCESS),granted_roles=('sdk_access_agent',),
        summarizer='summarize_sdk_access',timeout_sec=5)(read)
    registry.register(d,actor='system',reason='scoped SDK access archive')


class SDKAccessAgent(ForensicsAgent):
    role='sdk_access_agent';tools=('sdk_access_read',);source=EvidenceSource.CHANGES
    def plan_calls(self,ctx):
        if not current():return []
        return [ToolRequest(tool='sdk_access_read',params=dict(ctx.query_window(),service=s,limit=50),
            signal='sdk_access_history',rationale='核查留存 SDK 接口访问失败/恢复；历史不证明此刻状态') for s in target_services(ctx)]
    def build_evidence(self,ctx,request,result):
        return [from_event(r,self.role,r['raw_ref'],(ctx.window_start,ctx.window_end)) for r in result.data.get('events',[])]
    def summarize_empty(self,request,result):return '该窗口没有留存的 SDK 访问观测，不证明接口正常'
