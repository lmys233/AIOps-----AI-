"""Request and task ceilings enforced without granting new allowance."""
import os
from .contracts import metadata,cost
from ..orchestration.bootstrap import _limits_with_request_options

def task_limits():
    defaults={'tokens':200000,'tool_calls':80,'llm_calls':40,'wall_ms':300000}
    env={'tokens':'TOKENS','tool_calls':'TOOLS','llm_calls':'MODEL_CALLS','wall_ms':'WALL_MS'}
    for k,v in defaults.items():
        try:defaults[k]=max(0,min(v,int(os.getenv('AIOPS_TASK_MAX_'+env[k],str(v)))))
        except ValueError:pass
    return defaults

def budget_for(service,request,source=None):
    budget=_limits_with_request_options(None,request.options,service.router)
    used=metadata(source or {}).get('task_used') or cost(source or {});limits=task_limits()
    budget.max_tokens=min(budget.max_tokens,max(0,limits['tokens']-used.get('tokens',0)-used.get('reserved_tokens',0)))
    budget.max_tool_calls=min(budget.max_tool_calls,max(0,limits['tool_calls']-used.get('tool_calls',0)))
    budget.max_llm_calls=min(budget.max_llm_calls,max(0,limits['llm_calls']-used.get('llm_calls',0)))
    budget.max_wall_time_sec=min(budget.max_wall_time_sec,max(0,(limits['wall_ms']-used.get('wall_ms',0))/1000))
    return budget

def stamp(body,usage=()):
    from ..llm.accounting import summary,record_data
    ex=body.setdefault('explanation',{});d=ex.get('dialogue')
    totals=summary(usage);ex['model_usage']=totals
    ex['model_calls']=[record_data(r) for r in usage]
    used=body.setdefault('budget',{}).setdefault('used',{})
    execution=ex.get('execution') or {}
    used.update(tokens=(execution.get('used') or {}).get('tokens',totals['reported_tokens']),
        llm_calls=(execution.get('used') or {}).get('llm_calls',totals['physical_attempts']),
        reserved_tokens=(execution.get('reservations') or {}).get('unknown_model_tokens',totals['reserved_tokens']))
    if not totals['usage_known']:
        answer=ex.get('answer') or {}
        answer.setdefault('details',[]).append({'label':'模型计量',
            'value':f"已报告 {totals['reported_tokens']} token；{totals['unknown_calls']} 个逻辑调用用量未完整报告，另保留 {totals['reserved_tokens']} token预算估算。该预留不是供应商账单，不能把未知费用当作零。"})
    if not d and not ex.get('attribution_v2'):return
    if not d:
        d={'task_id':body['incident_id'],'action':'new_task','revision':body['incident_id'],
           'based_on_incident':'','based_on_revision':'','task_used':{},'completed':True}
        ex['dialogue']=d
    d['llm_calls']=totals['physical_attempts'];now=cost(body);before=dict(d.get('task_used') or {})
    if any(getattr(r,'role','')=='task_dialogue' for r in usage) and d.get('decision_source')!='fallback':
        d['decision_source']='model'
    d['task_used']={k:before.get(k,0)+v for k,v in now.items()};d['task_limits']=task_limits()
    parent=d.get('parent_used') or {}
    d['lineage_used']={k:parent.get(k,0)+v for k,v in d['task_used'].items()}
