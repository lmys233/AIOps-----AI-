"""Bound purpose/scope passed as data, never reclassified synthetic prose."""
from ..common.contracts import Slots,IntentResult
from ..common.enums import Intent,IntentSource
from ..intent.router import RouteOutcome,RouteTrace
from ..intent.rules import RuleHit


def bound(task,purpose=None):
    slots=Slots.model_validate(task.slots)
    kind=Intent(purpose or task.purpose)
    hit=RuleHit(kind,1,'task_bound',('持久任务目的；本轮仅更新范围/动作',))
    result=IntentResult(intent=kind.value,confidence=.9,confidence_raw=1,slots=slots,
        source=IntentSource.RULE,route='task_bound',latency_ms=0)
    return RouteOutcome(result=result,trace=RouteTrace(stages=('task_bound',),rule=hit))


def remaining_budget(service,request,task):
    from ..dialogue.limits import budget_for
    return budget_for(service,request,{'explanation':{'dialogue':{'task_used':task.used}}})


def patch_for(message,router,task):
    from ..intent.slots import extract_slots
    value=extract_slots(message,config=router.slot_config)
    patch={}
    if value.slots.service_names:
        patch.update(service_names=value.slots.service_names,
            service=value.slots.service.model_dump(mode='json') if value.slots.service else None)
    if 'time_window' not in value.unresolved:patch['time_window']=value.slots.time_window
    return patch
