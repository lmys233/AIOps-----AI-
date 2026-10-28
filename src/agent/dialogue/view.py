"""Per-turn answer and saved diagnosis are separate; exports use the same answer."""
from copy import deepcopy
from datetime import datetime,timezone
from .contracts import metadata,snapshot,cost
from ..presentation.contract_body import empty_contract_body
from ..common.turns import ensure_turn_id

def compatible(body):
    from ..presentation.projection import enabled
    return body if enabled() else canonical(body)

def canonical(body):
    seen={};aliases={};evidence=[]
    for e in body.get('evidence',[]):
        observation=next((t for t in e.get('tags',[]) if t.startswith('observation_id=')),None)
        key=(observation or e['evidence_id'],e.get('signal'))
        if key in seen:
            aliases[e['evidence_id']]=seen[key]
            canonical_item=next(x for x in evidence if x['evidence_id']==seen[key])
            canonical_item.setdefault('tags',[]).append('also_read_by='+str(e.get('agent_role','')))
            continue
        seen[key]=e['evidence_id'];evidence.append(e)
    body['evidence']=evidence
    if aliases:
        def visit(value,key=''):
            if isinstance(value,dict):return {k:visit(v,k) for k,v in value.items()}
            if isinstance(value,list):
                result=[visit(v,key) for v in value]
                refs=key in ('evidence_ids','supporting_evidence_ids','refuting_evidence_ids','citation_ids')
                return list(dict.fromkeys(result)) if refs and all(isinstance(v,str) for v in result) else result
            if isinstance(value,str):
                for old,new in aliases.items():value=value.replace(old,new)
                import re
                value=re.sub(r'(D-[a-zA-Z0-9]+)(?:、\1)+',r'\1',value)
            return value
        body['explanation']=visit(body['explanation'])
        body['explanation']['evidence_aliases']=aliases
    return body

def response(request,trace_id,source,p,decision,summary,rows,budget,submissions=(),reason=''):
    body=empty_contract_body(request.session_id,trace_id=trace_id,turn_id=ensure_turn_id(request),
        status='clarify' if reason else 'abstained',intent_name='INCIDENT_RCA',write_allowed=False)
    body['intent'].update(source='rule',confidence=.9,explanation='当前任务内对话动作，不重新生成诊断')
    if decision.decision_source=='model':body['intent']['source']='large_model'
    body['slots']=deepcopy((source or {}).get('slots') or {})
    body['evidence']=deepcopy((source or {}).get('evidence') or [])
    if source:
        body['root_cause']=deepcopy(source.get('root_cause'));body['hypotheses']=deepcopy(source.get('hypotheses') or [])
        body['explanation']['attribution_v2']=deepcopy(snapshot(source))
        from ..attribution.v2.confidence import refresh,headline,assessment_rows
        from ..attribution.v2.contracts import Snapshot
        current=refresh(Snapshot.model_validate(body['explanation']['attribution_v2'])).model_dump(mode='json')
        body['explanation']['attribution_v2']=current
        if body.get('root_cause') and all(p['cause']['status']=='confirmed' for p in current['problems']):body['status']='concluded'
        if decision.action in ('explain_result','verify_target'):
            provisional=headline(current)
            if provisional:summary+=' '+provisional+'。'
            rows.extend(assessment_rows(current))
    body['explanation']['answer']={'source':'task_dialogue','summary':summary,'details':rows,'details_kind':'table',
                                   'attribution_display_version':1}
    if source:
        from ..attribution.v2.confidence import questions
        body['explanation']['answer']['followup_questions']=questions(body['explanation']['attribution_v2'])
    if p:
        rows.insert(0,{'label':'历史诊断范围','value':p['service']+' / '+p['env']+'；'+str(p['time_range'])+'；本轮说明留存诊断，未重新核查此刻状态。'})
    body['explanation']['export_generated_at']=datetime.now(timezone.utc).isoformat()
    body['explanation']['dialogue']={'task_id':decision.task_id or body['incident_id'],'action':decision.action,
        'problem_id':decision.problem_id,'candidate_id':decision.candidate_id,
        'requirement_key':decision.requirement_key,'revision':body['incident_id'],
        'based_on_incident':(source or {}).get('incident_id',''),'based_on_revision':decision.revision,
        'completed':not reason,'reason':reason or decision.reason,'submissions':list(submissions),
        'decision_source':decision.decision_source,
        'task_used':metadata(source or {}).get('task_used') or cost(source or {}),'query_history':metadata(source or {}).get('query_history',[])}
    for key in ('parent_used','parent_task_id','attempt'):
        if key in metadata(source or {}):body['explanation']['dialogue'][key]=deepcopy(metadata(source)[key])
    if reason and metadata(source or {}).get('pending_target'):
        body['explanation']['dialogue']['pending_target']=deepcopy(metadata(source)['pending_target'])
    body['report']=summary+'\n\n'+'\n'.join(r['label']+'：'+r['value'] for r in rows)
    body['budget'].update(limits=budget.limits(),used=budget.usage(),enforced={'read_only':True,'task_ceiling':True})
    body['termination']={'reason':'本轮对话已回答；保留诊断不确定性' if not reason else reason,'satisfied':not bool(reason)}
    if reason:body['clarification']={'questions':[{'question':reason,'choices':[],
        'plugin_id':'task_dialogue','slot':'problem_id'}],'reason':reason,'plugin_id':'task_dialogue'}
    return compatible(body)
