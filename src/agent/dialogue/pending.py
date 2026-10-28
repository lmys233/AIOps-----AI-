"""Persist the purpose of a target question; selecting a record grants no evidence trust."""
from copy import deepcopy
import re
from .contracts import metadata,snapshot,task_id,Decision
from .routing import selection_ref,quick
from .view import response


def deferred_text(request,decision):
    from ..diagnostics.ingestion import redact
    if decision.action=='submit_evidence':
        from .submissions import clean_text
        import json
        clean,_=clean_text(request.message)
        return '补充数据：\n'+(json.dumps(clean,ensure_ascii=False) if isinstance(clean,dict) else clean)
    return redact(request.message)[:4000]


def ask_target(request,trace_id,message,budget,rows,decision):
    source=rows[0] if rows else None
    d=Decision(action='clarify_target',reason=message,task_id=task_id(source) if source else '')
    body=response(request,trace_id,source,None,d,message,[],budget,
                  metadata(source or {}).get('submissions',[]),reason=message)
    if source and decision.action!='clarify_target':
        foreground=decision.task_id or task_id(source)
        refs=[{'task_id':task_id(b),'revision':metadata(b).get('revision') or b['incident_id'],
               'problem_ids':[p['problem_id'] for p in snapshot(b)['problems']]}
              for b in rows if foreground in (task_id(b),b['incident_id'])]
        body['explanation']['dialogue']['pending_target']={
            'action':decision.action,'request_text':deferred_text(request,decision),'targets':refs,
            'question_revision':body['incident_id']}
    body['clarification']['questions'][0].update(plugin_id='task_dialogue',slot='problem_id')
    return body


def _matching(pending,request):
    ids=re.findall(r'\b(?:P-|H-|inc_)[a-zA-Z0-9]+\b',request.message)
    target=request.context.dialogue
    if target:ids.extend(v for v in (target.task_id,target.problem_id,target.candidate_id) if v)
    pids={i for i in ids if i.startswith('P-')};tids={i for i in ids if i.startswith('inc_')}
    if len(pids)>1 or len(tids)>1:return []
    if tids=={pending.get('question_revision')}:tids=set()
    return [r for r in pending.get('targets',[]) if
        (not tids or r['task_id'] in tids or r['revision'] in tids) and
        (not pids or pids<=set(r['problem_ids']))]


async def resume(service,request,rows,decision):
    if not selection_ref(request.message):return request,rows,decision
    choices=[]
    for b in rows:
        pending=metadata(b).get('pending_target')
        if pending:
            choices.extend((pending,r) for r in _matching(pending,request))
    if not choices:
        recovered=await legacy(service,request)
        if recovered:
            pending,source=recovered
            if not any(task_id(b)==task_id(source) for b in rows):rows=rows+[source]
            choices=[(pending,r) for r in _matching(pending,request)]
    if len(choices)!=1:
        if choices:decision.reason='多个待办包含该问题，请同时指定澄清事故/任务编号；本轮未查询。'
        return request,rows,decision
    pending,ref=choices[0]
    restored=Decision(action=pending['action'],task_id=ref['task_id'],
        reason='恢复选择问题前的推进目的；选择对象不代表确认原因')
    # Perform the deferred purpose, while archives still record the user's selection.
    effective=request.model_copy(update={'message':pending['request_text']})
    if restored.action in ('explain_gaps','explain_result','compare_candidates'):
        effective=request
    # The current reply supplies identifiers; binding and version checks remain mandatory.
    ids=re.findall(r'\b(?:P-|H-)[a-zA-Z0-9]+\b',request.message)
    restored.problem_id=next((i for i in ids if i.startswith('P-')),'')
    restored.candidate_id=next((i for i in ids if i.startswith('H-')),'')
    return effective,rows,restored


async def legacy(service,request):
    store=getattr(getattr(service,'history',None),'store',None)
    read=getattr(store,'dialogue_context',None)
    if not callable(read):return None
    try:turns=await read(request.session_id)
    except Exception:return None
    index=0
    if turns and selection_ref(turns[0].get('user_message','')):index=1
    if len(turns)<index+2:return None
    turn=turns[index];body=turn.get('response') or {};d=metadata(body)
    # Only the known legacy target question, adjacent to its original diagnosis.
    if snapshot(body) or d.get('action')!='clarify_target':return None
    reason=(body.get('clarification') or {}).get('reason','')
    if not reason.startswith('请指定服务或问题；'):return None
    original=turns[index+1].get('response') or {}
    if original.get('session_id')!=request.session_id or not snapshot(original).get('problems'):return None
    action=quick(turn.get('user_message',''))
    if not action or action.action in ('new_task','clarify_target'):return None
    named=set(re.findall(r'\bP-[a-zA-Z0-9]+\b',reason))
    valid=[p['problem_id'] for p in snapshot(original)['problems'] if p['problem_id'] in named]
    pending={'action':action.action,'request_text':turn['user_message'],'targets':[
        {'task_id':task_id(original),'revision':metadata(original).get('revision') or original['incident_id'],
         'problem_ids':valid}]}
    return (pending,original) if _matching(pending,request) else None
