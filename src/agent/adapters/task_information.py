"""Task-level information is read-only; directed verification still binds one problem."""
import re
from ..dialogue.contracts import metadata,snapshot
from ..dialogue.gaps import gaps,explain
from ..dialogue.candidate_review import prepare
from ..dialogue.view import response


def task_source(rows,request,decision):
    if decision.action not in ('explain_gaps','explain_result') or not rows:return None
    target=request.context.dialogue
    if decision.problem_id or decision.candidate_id or target and (target.problem_id or target.candidate_id):return None
    if re.search(r'\b(?:P-|H-)[a-zA-Z0-9]+\b',request.message):return None
    source=next((b for b in rows if not decision.task_id or metadata(b).get('task_id')==decision.task_id),None)
    if not source or len(snapshot(source).get('problems',[]))<2:return None
    broad=re.search('所有|全部|各个|整个任务',request.message)
    if metadata(source).get('problem_id') and not broad:return None
    return source


def information(service,request,trace_id,source,decision,budget):
    problems=snapshot(source).get('problems') or [];rows=[]
    if not problems:return non_diagnostic(request,trace_id,source,decision,budget)
    for problem in problems[:10]:
        reviewed,_=prepare(source,problem,decision)
        head,details=explain(reviewed,gaps(service,reviewed,decision),decision.action)
        label=problem['service']+' · '+problem['error_type']+' · '+problem['problem_id']
        rows.append({'label':'诊断分组 · '+label,'value':head})
        rows.extend({'label':label+' · '+r['label'],'value':r['value']} for r in details if r['label'].startswith(('优先补充','确认与排除','资料边界')))
    if len(problems)>10:rows.append({'label':'展示范围','value':'本轮展示前 10 个分组；其余可通过问题编号说明缺口。'})
    head=f'当前任务包含 {len(problems)} 个诊断分组。下表分别说明证据与所需资料；这些分组不等于已确认的形成原因，本轮未重新取证。'
    return response(request,trace_id,source,None,decision,head,rows,budget,metadata(source).get('submissions',[]))


def non_diagnostic(request,trace_id,source,decision,budget):
    from copy import deepcopy
    answer=(source.get('explanation') or {}).get('answer') or {}
    summary=answer.get('summary') or source.get('report') or '上一轮尚未得到可解释结果。'
    head='以下说明当前任务留存的结果与缺口，本轮未重新取证。\n'+summary
    rows=deepcopy(answer.get('details') or [])[:15]
    body=response(request,trace_id,source,None,decision,head,rows,budget)
    body['intent']['name']=(source.get('intent') or {}).get('name','INCIDENT_RCA')
    if source.get('clarification'):body['clarification']=deepcopy(source['clarification']);body['status']='clarify'
    return body
