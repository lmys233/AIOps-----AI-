"""Explicit requests can propose bounded candidates; proposals never confirm."""
from copy import deepcopy
from .contracts import snapshot
from .progress import state_for
from ..attribution.v2.contracts import Snapshot
from ..attribution.v2.broker import Broker
from ..attribution.v2.candidates import generate,prompt,Drafts
from ..attribution.v2.catalog import inventory,defer
from ..attribution.v2.contracts import Candidate
from ..common.tokens import estimate_tokens
from ..common.model_budget import model_budget_scope
import asyncio

async def propose(service,request,body,p,budget,trace_id,on_event,incident_id):
    gateway=getattr(service.router,'gateway',None)
    if not gateway or len(inventory(p,snapshot(body)))>=12 or budget.max_llm_calls<=budget.llm_calls:
        return body,'候选模型不可用或候选额度已满；保留已有可验证候选。'
    state=await state_for(service,request,body,p,budget,trace_id,on_event,incident_id)
    if service.orchestrator.executor is None:return body,'没有受权验证工具；不生成无法验证的原因结论。'
    broker=Broker(service.orchestrator,state)
    snap=Snapshot(problems=[deepcopy(next(x for x in Snapshot.model_validate(snapshot(body)).problems if x.problem_id==p['problem_id']))])
    snap.candidate_backlog={p['problem_id']:[Candidate.model_validate(x) for x in snapshot(body).get('candidate_backlog',{}).get(p['problem_id'],[])]}
    estimate=estimate_tokens(prompt(snap,list(broker.definitions)))+estimate_tokens(str(Drafts.model_json_schema()))
    available=budget.max_tokens-budget.tokens-estimate
    if available<1024:return body,'剩余 token 不足；保留现有候选和缺口。'
    budget.spend_llm(calls=1)
    with model_budget_scope(budget):
        await asyncio.wait_for(generate(snap,gateway=gateway,model=getattr(service.router,'main_model',''),
            tools=broker.definitions,max_tokens=min(2048,available)),timeout=max(.01,budget.max_wall_time_sec))
    updated=deepcopy(body);parent=Snapshot.model_validate(snapshot(body))
    problem=next(x for x in parent.problems if x.problem_id==p['problem_id'])
    existing={c.mechanism_key for c in problem.candidates}
    for entry in inventory(snap.problems[0].model_dump(mode='json'),snap.model_dump(mode='json')):
        c=Candidate.model_validate(entry['candidate'])
        if c.mechanism_key not in existing:defer(parent,problem,c)
    updated['explanation']['attribution_v2']=parent.model_dump(mode='json')
    return updated,'已处理新候选请求；新增解释仍须独立证据验证，本轮未取证或确认新原因。'
