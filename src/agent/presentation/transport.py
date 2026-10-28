"""Opt-in read projection; never passed back to execution or persistence."""
from copy import deepcopy

PARTS={'evidence':('evidence',),'problems':('explanation','attribution_v2','problems'),
       'observations':('explanation','attribution_v2','observations'),
       'history':('explanation','attribution_v2','history')}
EX_FIELDS=('answer','observed_system','diagnosis_ref','task_control','dialogue','execution','task_memory','model_context','plan_scope',
           'selected_plan_id','remediation_plans','advisor_outcome','model_calls','model_usage','internal_report',
           'unresolved_entities','reference','escalation','scoring','scoring_kind','intent_plugin','alert_ref','warning_refs','command_chain')


def response_mode(payload):
    options=payload.get('options')
    if options is None:return 'full'
    if not isinstance(options,dict):raise ValueError('options 必须为对象')
    mode=options.get('response_mode','full')
    if mode not in ('full','view'):raise ValueError('response_mode 必须为 full/view')
    return mode


def items(body,part):
    value=body
    for key in PARTS[part]: value=(value or {}).get(key)
    return value if isinstance(value,list) else []


def project(body,mode='full'):
    if mode=='full': return body
    if mode!='view': raise ValueError('response_mode 必须为 full/view')
    if body.get('payload_view'): return deepcopy(body)
    ex=body.get('explanation') or {};answer=ex.get('answer') or {}
    oid=(ex.get('observed_system') or {}).get('observed_id')
    if not oid or answer.get('view_version')!=1 or (ex.get('task_control') or {}).get('commit_state')!='committed':
        return dict(deepcopy(body),payload_view={'version':1,'mode':'full_fallback',
            'reason':'缺少固定答案版本或未确认持久档案，保留实际返回内容，不生成分页指针。'})
    result={k:deepcopy(v) for k,v in body.items() if k not in ('explanation','evidence','report','hypotheses','_payload_counts')}
    result['explanation']={k:deepcopy(ex[k]) for k in EX_FIELDS if k in ex and ex[k] is not None}
    result.update(evidence=[],hypotheses=deepcopy((body.get('hypotheses') or [])[:6]),report=answer.get('summary',''))
    counts=body.get('_payload_counts') or {part:len(items(body,part)) for part in PARTS}
    iid=body['incident_id']
    root='/v1/observed-systems/'+oid+'/incidents/'+iid if oid else '/v1/incidents/'+iid
    result['payload_view']={'version':1,'mode':'view','content_source':'saved_incident',
        'view_id':answer['view_id'],'diagnosis_ref':deepcopy(answer['diagnosis_ref']),
        'counts':counts,'full_href':root,'parts':{p:root+'/payload/'+p for p in PARTS},
        'note':'页面先读取保存答案；原始证据与验证历史按需分页，未随本次传输不代表没有证据。'}
    result['evidence_total']=counts['evidence']
    return result


def array_sql(expr,path):
    field=expr+"#>'{"+','.join(path)+"}'"
    return f"CASE WHEN jsonb_typeof({field})='array' THEN {field} ELSE '[]'::jsonb END"


def summary_sql(expr):
    # Only internal literal expressions, never request input.
    if expr not in ('body',"payload->'response'"): raise ValueError('invalid projection source')
    expr='('+expr+')'
    ex=f"({expr}->'explanation')"
    fields=','.join("'"+k+"',"+ex+"->'"+k+"'" for k in EX_FIELDS)
    counts=','.join("'"+p+"',jsonb_array_length("+array_sql(expr,path)+')' for p,path in PARTS.items())
    return f"""CASE WHEN {expr}#>>'{{explanation,answer,view_version}}'='1'
      AND {expr}#>>'{{explanation,task_control,commit_state}}'='committed' THEN
      ({expr}-'evidence'-'report'-'explanation'-'hypotheses') || jsonb_build_object(
        'explanation',jsonb_build_object({fields}),
        'hypotheses',COALESCE({expr}->'hypotheses','[]'::jsonb),
        '_payload_counts',jsonb_build_object({counts})) ELSE {expr} END"""
