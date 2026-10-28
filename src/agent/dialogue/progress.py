"""Bounded directed checks reuse the original Broker and verification engine."""
from copy import deepcopy
from ..common.contracts import Slots,Evidence
from ..common.enums import Intent
from ..agents.base import AgentOutcome
from ..board.store import InMemoryBoardStore
from ..orchestration.state import GraphState
from ..attribution.v2.contracts import Snapshot
from ..attribution.v2.broker import Broker
from ..attribution.v2.verification import Observation
from ..attribution.v2.runtime import apply_verdict
from ..orchestration.orchestrator import OrchestrationResult
from .contracts import snapshot,metadata
from ..attribution.v2.catalog import inventory,activate
import asyncio
import json
import re

async def state_for(service,request,body,p,budget,trace_id,on_event,incident_id,task_control=None):
    slots=Slots(service_names=[p['service']],env=p['env'],cluster=p.get('cluster') or None,
        time_window=body.get('slots',{}).get('time_window'),symptom=p['fact']['statement'])
    state=GraphState(incident_id,request.session_id,trace_id,request.message,slots,budget=budget)
    board=InMemoryBoardStore();state.flags.update(board_store=board,on_event=on_event,usage=[],
        dry_run_only=request.options.dry_run_only,deployment_form=service.deployment_form)
    state.board=await board.create(incident_id,slots)
    if task_control:state.flags['task_control']=task_control
    for raw in body.get('evidence',[]):
        e=Evidence.model_validate(raw)
        if e.entity.name!=p['service']:continue
        await board.publish(incident_id,str(e.agent_role),[e])
        state.outcomes.append(AgentOutcome(role=str(e.agent_role),evidence=[e]))
    return state

def selected(p,decision,text):
    cs=p['candidates']
    if decision.candidate_id:return [c for c in cs if c['candidate_id']==decision.candidate_id]
    if decision.requirement_key:return [c for c in cs if any(r['key']==decision.requirement_key for r in c['requirements'])]
    terms=re.findall(r'停机|期限|超时|断开|锁等待|迁移|删除|连接|线程|工作池|schema',text,re.I)
    preferred={'停机':'request_shutdown_cancel','期限':'request_configured_deadline','断开':'request_client_disconnect'}
    keys={preferred[t] for t in terms if t in preferred}
    exact=[c for c in cs if c['mechanism_key'] in keys]
    if exact:return exact
    matches=[c for c in cs if any(t in c['statement']+' '+c['mechanism_key'] for t in terms)]
    # Verification is not a vote for one explanation. Keep alternatives open.
    return matches if matches else [c for c in cs if c['status']!='excluded'][:4]

def prior_observations(body):
    rows=list(snapshot(body).get('observations',[]));seen=set()
    for e in body.get('evidence',[]):
        value=next((t.split('=',1)[1] for t in e.get('tags',[]) if t.startswith('observation_json=')),None)
        if value:rows.append(json.loads(value))
    out=[]
    for r in rows:
        o=Observation.model_validate(r)
        key=o.evidence_id or json.dumps(o.model_dump(mode='json'),sort_keys=True,ensure_ascii=False)
        if key not in seen:out.append(o);seen.add(key)
    return out

async def verify(service,request,body,p,decision,budget,trace_id,on_event,incident_id,task_control=None):
    reviewed=dict(p,candidates=[x['candidate'] for x in inventory(p,snapshot(body))])
    chosen=selected(reviewed,decision,request.message)
    if not chosen:return body,0,[],'请指定候选或要核查的条件；本轮未查询。'
    state=await state_for(service,request,body,p,budget,trace_id,on_event,incident_id,task_control)
    if service.orchestrator.executor is None:return body,0,[],'只读执行器未配置，本轮未查询。'
    broker=Broker(service.orchestrator,state)
    base=Snapshot.model_validate(snapshot(body))
    if not activate(base,p['problem_id'],chosen):return body,0,[],'候选队列无可安全换位的额度，保留待查项；本轮未查询。'
    from ..application.diagnosis_workflow import run
    from ..domain.diagnosis import DiagnosisCommand
    command=DiagnosisCommand(mode='directed',problem_id=p['problem_id'],
        candidate_ids=tuple(x['candidate_id'] for x in chosen),requirement_key=decision.requirement_key,
        recheck=bool(re.search('重新|新记录|补充|新采集|已上传|已接入',request.message)))
    observations=prior_observations(body);readable={e.evidence_id for e in state.evidence}
    outcome=await run(base,command=command,tools=broker.definitions,fetch=broker.fetch,
        readable=readable,budget=budget,observations=observations,
        visited=metadata(body).get('query_history',[])+base.source.get('diagnosis_workflow',{}).get('queries',[]),availability=broker.availability,
        on_plan=lambda value:setattr(broker,'snapshot',value))
    after,actual=outcome.snapshot,outcome.query_ids
    after.history.append({'dialogue':'verify_target','queries':actual,'based_on_incident':body['incident_id']})
    from .plan_projection import merge_evidence
    result=deepcopy(body)
    apply_verdict(state,after)
    from ..orchestration.facts import _root_cause_dict,_hypothesis_facts
    result.update(root_cause=_root_cause_dict(state.arbitration),hypotheses=_hypothesis_facts(state))
    result['evidence']=merge_evidence(body.get('evidence') or [],[e.model_dump(mode='json') for e in state.evidence])
    result['explanation']['attribution_v2']=after.model_dump(mode='json')
    return result,budget.tool_calls,actual,after.stop_reason

async def plans(service,request,body,p,budget,trace_id,usage,on_event,incident_id,task_control=None):
    from ..common.model_budget import model_budget_scope
    with model_budget_scope(budget,pre_reserved=False):
        route=await asyncio.wait_for(service.router.route(p['service']+' '+request.message,env=p['env'],
            cluster=p.get('cluster') or None),timeout=max(.01,budget.max_wall_time_sec))
    if route.intent is not Intent.REMEDIATION:raise ValueError('求方案未通过原 REMEDIATION 路由')
    state=await state_for(service,request,body,p,budget,trace_id,on_event,incident_id,task_control)
    state.route=route;state.intent=Intent.REMEDIATION;state.flags['usage']=usage
    from ..attribution.v2.selection import problem_snapshot
    snap=problem_snapshot(snapshot(body),p['problem_id'])
    state.flags['attribution_v2']=snap.model_dump(mode='json');apply_verdict(state,snap)
    await service.orchestrator._generate_ai_plans(state)
    service.orchestrator._prepare_action(state)
    state.status='clarify';result=OrchestrationResult(state,service.orchestrator._facts(state))
    facts=dict(result.facts);facts['answer']=await service.task_ports.answer_step(result,request.message,trace_id,on_event)
    rendered=service.task_ports.render(state,facts,request,trace_id=trace_id,usage=usage,degraded=[])
    from .plan_projection import preserve
    preserve(rendered,body,p)
    await service.task_ports.settle_turn(request,rendered,result,[],getattr(service.orchestrator,'session',None),request.message,'')
    return rendered
