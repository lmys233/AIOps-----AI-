"""One mandatory current check per SDK error interface; historical facts stay intact."""
import asyncio,hashlib,json,time
from datetime import datetime,timezone
from .context import current
from .sdk_access import target
from ..diagnostics.evidence import fault


def is_sdk_evidence(item):
    if any(t.startswith('error_type=SDK_ACCESS_') for t in item.tags):return True
    encoded=next((t.split('=',1)[1] for t in item.tags if t.startswith('diagnostic_event_json=')),None)
    return bool(encoded and json.loads(encoded).get('kind')=='sdk_access')


def failures(evidence,scope):
    groups={}
    for item in evidence:
        encoded=next((t.split('=',1)[1] for t in item.tags if t.startswith('diagnostic_event_json=')),None)
        if not encoded:continue
        row=json.loads(encoded);identity=row['identity']
        if row['kind']!='sdk_access' or not fault(row):continue
        if (identity.get('tenant'),identity.get('observed_id'),identity.get('env'))!=(scope.tenant,scope.observed_id,scope.env):continue
        if scope.cluster is not None and identity.get('cluster')!=scope.cluster:continue
        query=row['payload'].get('sdk_query',{})
        key=(identity['service'],row['payload']['interface_id'],json.dumps(query,sort_keys=True),identity.get('cluster') or '')
        groups.setdefault(key,[]).append(item.evidence_id)
    return groups


async def run(orch,state):
    started=time.monotonic();base=state.budget.elapsed_sec
    remaining=max(.001,state.budget.max_wall_time_sec-base)
    try:await asyncio.wait_for(_run(orch,state,started,base),remaining)
    except TimeoutError:
        status=state.flags.setdefault('sdk_verification',{'version':1,'checks':[]})
        status['stop_reason']='SDK强制核验到达本轮墙钟预算；未完成检查保持未知'
    finally:state.budget.observe_elapsed(base+time.monotonic()-started)


async def _run(orch,state,started,base):
    scope=current()
    if scope is None or not orch.executor or str(state.intent) not in ('INCIDENT_RCA','METRIC_QUERY','REMEDIATION'):
        return
    if state.flags.get('sdk_verification') is not None:return
    groups=failures(state.evidence,scope)
    if not groups:return
    status=state.flags['sdk_verification']={'version':1,'checks':[],'purpose':'核查SDK错误对应的当前接口，不判断历史原因或业务可用性'}
    from ..webprobe.sdk import descriptor,authorize
    from ..webprobe.models import RequestPlan
    from ..webprobe.workflow import call
    from ..execution.runtime import CURRENT
    execution=CURRENT.get()
    for (service,interface,query,cluster),basis in sorted(groups.items()):
        state.budget.observe_elapsed(base+time.monotonic()-started)
        check=dict(service=service,interface_id=interface,cluster=cluster or None,basis_ids=list(dict.fromkeys(basis)),state='not_queried')
        status['checks'].append(check)
        if state.budget.exhausted():check['reason']='强制SDK核验未执行：剩余预算不足';continue
        try:
            row=await target(scope,service,exact_cluster=cluster or None)
            selected=descriptor(row,interface)
            state.budget.observe_elapsed(base+time.monotonic()-started)
            plan=RequestPlan(**selected,query=json.loads(query),timeout_sec=2).model_dump(mode='json')
            runkey=(execution.rid if execution else state.incident_id)+':sdk:'+service+':'+interface+':'+query+':'+plan['endpoint_revision']+':'+row['boot_id']
            params=dict(plan,run_id='probe_'+hashlib.sha256(runkey.encode()).hexdigest()[:32])
            # A snapshot may already have been sampled in initial forensics. Respect
            # its SDK cooldown before the distinct mandatory web_query, not a retry.
            pause=0
            if interface=='snapshot':
                recent=[r for r in scope.sdk_access_checks.values() if r['identity']['service']==service and
                    r['identity']['boot_id']==row['boot_id'] and r['interface_id']==interface and r['ok']]
                if recent:
                    last=max(datetime.fromisoformat(r['checked_at']) for r in recent)
                    pause=max(0,2.05-(datetime.now(timezone.utc)-last).total_seconds())
            remaining=state.budget.max_wall_time_sec-state.budget.elapsed_sec
            if remaining<=pause+3 or state.budget.tool_calls>=state.budget.max_tool_calls:
                check['reason']='强制SDK核验未执行：冷却/访问所需预算不足';continue
            if pause:
                await asyncio.sleep(pause);state.budget.observe_elapsed(base+time.monotonic()-started)
            call_started=datetime.now(timezone.utc)
            with authorize(params):response=await call(orch,state,'web_query',params)
            fresh=any(r['tool']=='web_query' and r['identity']['service']==service and r['interface_id']==interface and
                datetime.fromisoformat(r['checked_at'])>=call_started for r in scope.sdk_access_checks.values())
            outcome=('verified' if fresh else 'saved_verified') if response.ok else ('failed' if fresh else 'not_queried')
            checked=(response.data.get('finished_at') if response.ok else None) or datetime.now(timezone.utc).isoformat()
            check.update(state=outcome,params=params,result=response.model_dump(mode='json'),checked_at=checked)
            if response.ok:check['reason']=('当前同一SDK接口HTTP 200且登记身份匹配；不能否定历史失败或证明旧boot恢复' if fresh else
                '执行台账复用已完成的SDK核验；保持原核查时刻，未重新发请求，不冒充当前响应')
            else:check['reason']=('具体SDK链路核验失败：' if fresh else '本次没有发出新的可校验SDK请求：')+response.summary
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception('SDK核验未能执行: service=%s interface=%s',service,interface)
            check['reason']='强制SDK核验无法执行：'+getattr(exc,'message',type(exc).__name__)
    # The code-level check does not consume cause-candidate or proof-query budgets.
    state.flags['sdk_verification']=status
