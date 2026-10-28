"""Compatibility entry; the application workflow owns diagnostic execution."""
import asyncio
from ...common.tokens import estimate_tokens
from .candidates import generate, prompt, Drafts
from ...application.diagnosis_workflow import Fetched, failed_rows, run as execute
from ...application.diagnosis_budget import budget_reason
from ...domain.diagnosis import DiagnosisCommand


async def run(snapshot, *, tools, fetch, readable, budget, gateway=None, model='',
              observations=(), on_update=None, availability=None, on_plan=None):
    async def candidates(value):
        await _generate(value, gateway, model, tools, budget)
    result = await execute(snapshot, command=DiagnosisCommand(), tools=tools, fetch=fetch,
        readable=readable, budget=budget, observations=observations, generate=candidates,
        on_update=on_update, availability=availability, on_plan=on_plan)
    return result.snapshot


async def _generate(snapshot,gateway,model,tools,budget):
    import hashlib,json
    from .candidates import seed_candidates
    seed_candidates(snapshot)
    if gateway is None:
        snapshot.notes.append('候选模型未接入；保留可验证规则候选与未知原因缺口');return
    attempted=snapshot.source.setdefault('candidate_model_attempts',[])
    receipts=snapshot.source.setdefault('candidate_generation',[])
    pending=[]
    for p in snapshot.problems:
        key=hashlib.sha256(json.dumps([p.problem_id,sorted(p.evidence_ids)]).encode()).hexdigest()
        if key not in attempted and len([c for c in p.candidates if c.status!='excluded'])<4:pending.append((p,key))
    pending=candidate_order(snapshot,pending)
    while pending:
        slots=12-sum(c.status!='excluded' for p in snapshot.problems for c in p.candidates)
        if slots<=0:
            snapshot.source['candidate_model_unqueried']=sorted(p.problem_id for p,key in pending)
            snapshot.notes.append('候选预算已满；其余模型候选未生成，不代表问题已排除');break
        batch=pending[:min(3,slots)];pending=pending[len(batch):]
        ids={p.problem_id for p,key in batch}
        focused=snapshot.model_copy(deep=True,update={'problems':[p.model_copy(deep=True) for p,key in batch],
            'candidate_backlog':{pid:items for pid,items in snapshot.candidate_backlog.items() if pid in ids}})
        remaining=budget.max_tokens-budget.tokens-getattr(budget,'reserved_tokens',0)
        estimate=estimate_tokens(prompt(focused,tools))+estimate_tokens(str(Drafts.model_json_schema()))
        if budget_reason(budget) or remaining-estimate<1024:
            snapshot.source['candidate_model_unqueried']=sorted(ids|{p.problem_id for p,key in pending})
            snapshot.notes.append('候选模型预算不足；未生成的问题仍需核查');break
        budget.spend_llm(calls=1)
        attempted.extend(key for p,key in batch);del attempted[:-64]
        before=len(focused.notes)
        try:
            from ...common.model_budget import model_budget_scope
            with model_budget_scope(budget):
                await asyncio.wait_for(generate(focused,gateway=gateway,model=model,tools=tools,
                    max_tokens=min(3072,remaining-estimate),new_limit=len(batch)),
                    timeout=max(.01,budget.max_wall_time_sec-budget.elapsed_sec))
            for p in focused.problems:
                original=next(x for x in snapshot.problems if x.problem_id==p.problem_id)
                original.candidates=p.candidates
                if p.problem_id in focused.candidate_backlog:
                    snapshot.candidate_backlog[p.problem_id]=focused.candidate_backlog[p.problem_id]
                if not p.candidates and p.metric_name:
                    gap=f'指标 {p.metric_name} 的含义/容量及与请求退化的关联尚未核验；变化本身不能确认形成原因'
                    if gap not in original.gaps:original.gaps.append(gap)
            snapshot.notes.extend(focused.notes[before:])
            receipt=focused.source.get('candidate_model_outcome',{'status':'unknown'})
        except TimeoutError:
            snapshot.notes.append('候选模型超过剩余耗时预算');receipt={'status':'failed','failure_kind':'stage_deadline'}
        receipts.append({'problem_ids':sorted(ids),**receipt});del receipts[:-32]


def candidate_order(snapshot,pending):
    """One problem per service first; sample priorities never grant cause authority."""
    ranks=(snapshot.source.get('metric_localization') or {}).get('ranked_services',[])
    service_order=list(dict.fromkeys([r['service'] for r in ranks]+[p.service for p,key in pending]))
    metric_order={r['service']:r.get('metrics',[]) for r in ranks}
    queues={service:[row for row in pending if row[0].service==service] for service in service_order}
    for service,rows in queues.items():
        names=metric_order.get(service,[])
        rows.sort(key=lambda row:names.index(row[0].metric_name) if row[0].metric_name in names else len(names))
    ordered=[]
    while any(queues.values()):
        for service in service_order:
            if queues[service]:ordered.append(queues[service].pop(0))
    return ordered
