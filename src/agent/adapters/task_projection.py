"""Existing response contracts translated at the infrastructure boundary."""
from copy import deepcopy
from ..domain.tasks import Task,PlanRef
from ..memory.gates import redact_text


def shown_plans(body):
    from ..api.tasks import plans_of_body
    return plans_of_body(body)


def task_state(body):
    if body.get('status')=='failed':return 'failed'
    if (body.get('clarification') or {}).get('questions'):
        questions=body['clarification']['questions']
        slots=[q.get('slot','') for q in questions]
        return 'awaiting_scope' if any(k in slots for k in ('service','env','time_window')) else 'awaiting_target'
    ex=body.get('explanation') or {}
    if ex.get('remediation_plans') or ex.get('ai_plans'):return 'ready_for_plan'
    problems=(ex.get('attribution_v2') or {}).get('problems') or []
    return 'awaiting_evidence' if any(p['cause']['status']!='confirmed' for p in problems) else 'completed'


def project(task,body,*,imported=False):
    t=task.model_copy(deep=True);ex=body.get('explanation') or {};meta=ex.get('dialogue') or {}
    if t.purpose is None:t.purpose=(body.get('intent') or {}).get('name')
    if imported:t.purpose=(body.get('intent') or {}).get('name')
    t.original=redact_text(t.original)[0];t.slots=deepcopy(body.get('slots') or t.slots)
    t.last_incident_id=body['incident_id'];t.legacy_id=t.legacy_id or meta.get('task_id') or body['incident_id']
    t.state=task_state(body);t.missing=[q.get('slot','') for q in (body.get('clarification') or {}).get('questions',[])]
    t.pending=deepcopy(meta.get('pending_target') or {})
    ps=(ex.get('attribution_v2') or {}).get('problems') or []
    if ps:
        t.diagnosis_id=body['incident_id'];t.problem_ids=[p['problem_id'] for p in ps]
        from ..attribution.v2.catalog import inventory
        snap=ex['attribution_v2']
        t.candidate_refs={p['problem_id']:[x['candidate']['candidate_id'] for x in inventory(p,snap)] for p in ps}
    if 'problem_id' in meta:t.problem_id=meta['problem_id']
    if 'candidate_id' in meta:t.candidate_id=meta['candidate_id']
    plans=shown_plans(body)
    if plans:
        t.plan_source_id=body['incident_id']
        t.plans=[PlanRef(option_id=str(p.get('option_id') or i+1),title=str(p.get('title') or ''),action_class=str(p.get('action_class') or '')) for i,p in enumerate(plans)]
    c=body.get('cost') or {}
    t.used=deepcopy(meta.get('task_used') or dict(tokens=c.get('input_tokens',0)+c.get('output_tokens',0),tool_calls=c.get('tool_calls',0),llm_calls=len(ex.get('model_calls') or []),wall_ms=c.get('wall_ms',0)))
    execution=ex.get('execution') or {}
    if not (ex.get('model_usage') or {}).get('usage_known',True):t.budget_unknown=True
    if ex.get('task_memory'):
        t.memory.update(deepcopy(ex['task_memory']))
        t.state=task.state
    t.memory={**t.memory,'goal':t.original,'purpose':t.purpose,'slots':deepcopy(t.slots),
            'diagnosis_id':t.diagnosis_id,'problem_ids':list(t.problem_ids),
            'candidate_refs':deepcopy(t.candidate_refs),'missing':list(t.missing),
            'execution_id':t.execution_id}
    if ex.get('active_probe'):
        t.memory['active_probe']=deepcopy(ex['active_probe'])
        if ex['active_probe'].get('action')=='ask':
            t.state='awaiting_target';t.missing=['endpoint']
    if ex.get('warning_refs'):t.memory['warning_refs']=deepcopy(ex['warning_refs'])
    if execution:
        t.memory.update(query_anchor=execution.get('query_anchor'),resume_node=execution.get('resume_node'))
        if execution.get('interrupted'):
            t.state='investigating';t.budget_unknown=True;t.used=deepcopy(task.used)
        else:
            # This execution's cumulative ledger includes interrupted attempts; charge once.
            used=execution.get('used') or {}
            t.used={k:task.used.get(k,0)+used.get(k,0) for k in ('tokens','llm_calls','tool_calls','wall_ms')}
            t.used['wall_ms']+=(execution.get('reservations') or {}).get('unknown_tool_wall_ms',0)
            t.used['reserved_tokens']=task.used.get('reserved_tokens',0)+(execution.get('reservations') or {}).get('unknown_model_tokens',0)
            t.budget_unknown=bool(execution.get('budget_unknown'))
            if isinstance(meta,dict):meta['task_used']=deepcopy(t.used)
    return t


def source_for(task,body):
    body=deepcopy(body);meta=body.setdefault('explanation',{}).setdefault('dialogue',{})
    meta.update(task_id=task.task_id,revision=str(task.revision),task_used=task.used)
    meta['task_aliases']=[v for v in (task.legacy_id,task.last_incident_id) if v]
    if task.pending:meta['pending_target']=deepcopy(task.pending)
    return body
