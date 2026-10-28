"""Bounded propose/request/observe loop; integrates existing execution and diagnosis."""
from uuid import uuid4
import time
from datetime import datetime,timezone,timedelta
from ..common.tool_budget import tool_budget_scope
from ..observed.context import current
from ..orchestration.state import board_store
from ..agents.base import AgentOutcome
from ..diagnostics.evidence import from_event
from ..tools.errors import ToolRefused
from .models import RequestPlan,Decision
from .targets import resolve,consent,fingerprint
from .planning import decide

ELIGIBLE={'INCIDENT_RCA','METRIC_QUERY','REMEDIATION'}


def wanted(state):
    # Eligibility opens a model decision, never permission to send a request.
    # SDK history must not suppress observation of the actual business system.
    return str(state.intent) in ELIGIBLE


async def call(orch,state,tool,params,role='probe_agent'):
    cid='probe-tool-'+uuid4().hex
    emitter=lambda e,d:orch._emit(state,e,d)
    sdk=tool=='web_query' and params.get('target_kind')=='sdk'
    title='核验SDK具体HTTP链路' if sdk else {'web_query':'主动HTTP请求','endpoint_inventory_read':'读取已登记业务接口','probe_observations_read':'回取主动请求SDK观测'}.get(tool,'查询探测前后指标')
    payload=dict(call_id=cid,name=tool,title=title,params=params,role=role,purpose='sdk_verification' if sdk else 'active_observation',raw_ref='',
        rationale='代码强制核验错误记录对应的同一SDK接口；成功只否定当前仍不可达，不否定过去错误' if sdk else {'web_query':'仅对当前环境已登记且开放的候选接口发真实请求，观察实际结果',
            'endpoint_inventory_read':'按当前系统/环境读取接口清单与探测政策，避免猜路由或任意URL',
            'probe_observations_read':'只回取同run、同实例/boot的新SDK记录，和实际请求关联'}.get(tool,'比较负载前后滚动指标；可能含普通流量或尚未覆盖抓取'))
    emitter('tool',{**payload,'phase':'start'})
    tick=time.monotonic();base=state.budget.elapsed_sec
    try:
        with tool_budget_scope(state.budget):result=await orch.executor.call(role,tool,params)
    finally:state.budget.observe_elapsed(base+time.monotonic()-tick)
    emitter('tool',{**payload,'phase':'done','ok':result.ok,'result':result.data if result.ok else result.summary,
        'error_code':result.error_code,'elapsed_ms':result.elapsed_ms,'raw_ref':result.raw_ref})
    return result


def ask(state,question,*,reason='',pending=None,candidates=()):
    state.flags['active_probe'].update(action='ask',question=question,reason=reason,
        pending=pending,candidates=list(candidates),asked_at=datetime.now(timezone.utc).isoformat())


async def run(orch,state):
    if not current() or not orch.executor or not wanted(state) or state.flags.get('active_probe'):
        return
    state.flags['active_probe']={'version':1,'runs':[],'observations':[],'action':'stop'}
    activity=state.flags['active_probe']
    try:
        from ..execution.runtime import CURRENT
        execution=CURRENT.get()
        if execution:
            execution.body['probe_context']=state.flags.get('webprobe_context',{})
            await execution.save()
        listed=await call(orch,state,'endpoint_inventory_read',{})
        if not listed.ok:
            activity.update(action='ask',question='业务接口清单未能读取，请提供疑似接口或业务操作，并核查SDK接口登记。',reason=listed.summary);return
        inventory=(listed.data.get('records') or [{}])[0].get('instances',[])
        state.flags['active_probe']['inventory_summary']=[dict(service=i['service'],instance=i['instance'],discovery=i['discovery'],count=len(i['endpoints'])) for i in inventory]
        approved=state.flags.get('webprobe_context',{}).get('approved')
        if approved:
            if time.time()>approved['expires_at']:raise ValueError('待确认探测已过期，请重新规划')
            plans=[approved['params']];decision=Decision(action='probe',reason='用户确认先前完整写请求方案；不重新生成参数')
        else:
            if not any(i.get('endpoints') for i in inventory):
                from ..observed.sdk_verification import is_sdk_evidence
                observed=any(not is_sdk_evidence(e) and (str(e.direction) in ('up','spike') or
                    any(t.startswith('error_type=') for t in e.tags)) for e in state.evidence)
                question='哪个接口或业务操作疑似异常？当前没有已登记的业务接口，补齐登记后才能进行主动核验。'
                decision=Decision(action='observe' if observed or str(state.intent)=='METRIC_QUERY' else 'ask',
                    reason='当前登记目录没有业务接口；跳过无法执行的模型探测规划，继续被动取证；不代表业务正常。',
                    question='' if observed or str(state.intent)=='METRIC_QUERY' else question)
            else:decision=await decide(orch,state,inventory)
            import hashlib
            plans=[dict(p.model_dump(mode='json'),run_id='probe_'+hashlib.sha256(
                ((execution.rid if execution else state.incident_id)+':'+str(n)+':'+fingerprint(p.model_dump(mode='json'))).encode()).hexdigest()[:32])
                for n,p in enumerate(decision.requests)]
        activity['decisions']=[decision.model_dump(mode='json')]
        activity['candidate_endpoints']=[dict(service=i['service'],instance=i['instance'],method=e['method'],
            path=e['path'],description=e['description'],endpoint_id=e['id'],effect=e['policy']['effect'])
            for i in inventory for e in i['endpoints'] if e['id'] in decision.candidates][:10]
        if decision.action=='ask':ask(state,decision.question or '哪个接口或业务操作疑似异常？',reason=decision.reason,candidates=decision.candidates);return
        if decision.action!='probe':activity['reason']=decision.reason;return
        if not plans:raise ValueError('模型没有提供可执行请求')
        if sum(p['max_requests'] for p in plans)>200:raise ValueError('本轮总请求超过200')
        for params in plans:
            RequestPlan.model_validate({k:v for k,v in params.items() if k!='run_id'})
            row,endpoint,_=await resolve(params,require_consent=False)
            if endpoint['policy']['effect']=='write' and not approved:
                scope=current();pending=dict(params=params,scope=[scope.tenant,scope.observed_id,scope.env,scope.cluster],
                    fingerprint=fingerprint(params),expires_at=time.time()+900,method=endpoint['method'],path=endpoint['path'])
                ask(state,'该接口会产生业务写入。请核对下方完整参数；同意本次请求请回复“确认探测 '+params['run_id']+'”。超时可能已生效，不会自动重发。',
                    reason='业务写请求的单次确认',pending=pending);return
            metric_before=await metrics(orch,state,params['service'],'before') if params['mode']=='load' else None
            with consent(approved):response=await call(orch,state,'web_query',params)
            activity['runs'].append(dict(params=params,result=response.model_dump(mode='json'),metric_before=metric_before))
            if not response.ok:
                activity['reason']=response.summary;continue
            observation_params={k:params[k] for k in ('service','instance','boot_id','run_id')}
            observed=await call(orch,state,'probe_observations_read',dict(observation_params,poll=0))
            if observed.ok and not observed.data.get('events') and not state.budget.exhausted():
                # One finite settle interval, never a loop that repeats business requests.
                import asyncio
                await asyncio.sleep(.25)
                observed=await call(orch,state,'probe_observations_read',dict(observation_params,poll=1))
            activity['runs'][-1]['sdk_observation']=observed.model_dump(mode='json')
            if observed.ok:
                sample=observed.data.get('events',[])
                reqids={r['request_id'] for r in response.data['results']}
                sample=[e for e in sample if e['correlation'].get('request_id') in reqids and e['identity']['boot_id']==row['boot_id']]
                evidence=[from_event(e,'probe_agent',observed.raw_ref) for e in sample]
                state.outcomes.append(AgentOutcome(role='probe_agent',evidence=evidence))
                board=board_store(state,orch.board,need='publish')
                if board:await board.publish(state.incident_id,'probe_agent',evidence)
                activity['observations'].extend(e['event_id'] for e in sample)
            else:state.degraded_detail.append('主动请求已发出，但SDK新采集未能回取：'+observed.summary)
            if params['mode']=='load':
                # Account for usual scrape interval without pretending an instant gauge is load QPS.
                import os,asyncio
                pause=min(20,max(0,float(os.getenv('AIOPS_PROBE_METRIC_SETTLE_SEC','5'))))
                if state.budget.max_wall_time_sec-state.budget.elapsed_sec>pause+3:
                    tick=time.monotonic();base=state.budget.elapsed_sec
                    await asyncio.sleep(pause);state.budget.observe_elapsed(base+time.monotonic()-tick)
                activity['runs'][-1]['metric_after']=await metrics(orch,state,params['service'],'after')
            if state.budget.exhausted():break
        activity['action']='observed'
        if state.budget.reserved_tokens<=0 and not state.budget.exhausted():
            final=await decide(orch,state,inventory,activity['runs'])
            activity['decisions'].append(final.model_dump(mode='json'))
            if final.action=='ask':ask(state,final.question or '本次样本仍不足，请补充接口参数或出现异常的操作步骤。',reason=final.reason,candidates=final.candidates)
            elif final.action=='probe':activity['reason']='已达到本轮主动请求批次边界；新的复现条件需下一轮继续，未重复请求'
            else:activity['reason']=final.reason
        elif state.budget.reserved_tokens>0:
            activity['reason']='部分模型尝试用量未知；保留已取得主动样本，不再派发新的模型决策'
        else:activity['reason']=state.budget.exhausted() or '主动核验预算边界已到'
    except (ToolRefused,ValueError) as exc:
        ask(state,'请补充已登记接口的必要参数或确认探测范围。',reason=str(exc))
    except Exception as exc:
        # Failure doesn't turn into an assertion of no faults, or restart traffic.
        activity.update(reason='主动核验未完成：'+type(exc).__name__,failure=True)
        if not activity['runs']:ask(state,'哪个接口或业务操作疑似异常？请提供具体路径或描述。',reason=activity['reason'])


async def metrics(orch,state,service,stage):
    if not orch.executor.registry.get('promql_read') or state.budget.exhausted():return None
    end=datetime.now(timezone.utc);start=end-timedelta(seconds=60)
    query='sum by (service) (rate(http_request_duration_seconds_count{service="'+service+'"}[30s]))'
    result=await call(orch,state,'promql_read',dict(query=query,start=start.isoformat(),end=end.isoformat(),step='5s',limit=10),'metrics_agent')
    if result.ok:
        from ..agents.metrics_agent import MetricsAgent
        from ..agents.base import AgentContext,ToolRequest
        ctx=AgentContext(state.incident_id,state.slots.model_copy(update={'service_names':[service]}),start,end,current().tenant)
        evidence=MetricsAgent(snapshot=True).build_evidence(ctx,ToolRequest(tool='promql_read',params={},signal='qps'),result)
        for e in evidence:
            e.summary='[主动负载'+('前' if stage=='before' else '后')+'滚动窗口，可能含普通流量] '+e.summary
            e.tags.insert(0,'active_probe_metric_stage='+stage)
            e.entity=e.entity.model_copy(update={'observed_id':current().observed_id})
        state.outcomes.append(AgentOutcome(role='metrics_agent',evidence=evidence))
        board=board_store(state,orch.board,need='publish')
        if board:await board.publish(state.incident_id,'metrics_agent',evidence)
    return dict(result=result.model_dump(mode='json'),range=[start.isoformat(),end.isoformat()],
        note='指标rate滚动窗口与抓取周期可能混入历史/尚未覆盖负载；不等同本次客户端QPS')
