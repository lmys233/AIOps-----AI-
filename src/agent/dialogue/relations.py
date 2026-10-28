"""Other faults are leads requiring a propagation path, not automatic cause candidates."""
import re
from .contracts import snapshot
from ..attribution.v2.display_groups import groups,title

RELATION=re.compile(r'关联|有关|相关|影响|导致|造成|引起|could.{0,20}cause|related',re.I)


def focal(body):
    from .focus import matches
    from .contracts import metadata
    ps=snapshot(body)['problems'];pid=metadata(body).get('problem_id')
    if pid:return next((p for p in ps if p['problem_id']==pid),None)
    slots=body.get('slots') or {};named=slots.get('service_names') or []
    found=matches([p for p in ps if len(named)!=1 or p['service']==named[0]],slots.get('symptom') or '')
    return found[0] if len(found)==1 else None


def linked_target(rows,request,decision,ids):
    if not RELATION.search(request.message) or decision.action not in ('explain_gaps','compare_candidates','explain_result'):return None
    scoped=[b for b in rows if not decision.task_id or decision.task_id in
            (b['incident_id'],(b.get('explanation') or {}).get('dialogue',{}).get('task_id'))]
    if not scoped:return None
    body=scoped[0];known={p['problem_id'] for p in snapshot(body)['problems']}
    if not set(ids)<=known:return None
    explicit=request.context.dialogue
    return (explicit.problem_id if explicit and explicit.problem_id in known else
            (focal(body) or {}).get('problem_id'))


def review(source,p,text):
    ps=snapshot(source)['problems'];explicit=[q['service'] for q in ps if q['service'] in text]
    candidates=[q for q in ps if q['problem_id']!=p['problem_id'] and q['error_type']!=p['error_type']
        and q.get('env')==p.get('env') and q.get('cluster')==p.get('cluster')
        and (q['service']==p['service'] or q['service'] in explicit)]
    out=[]
    for members in groups({'problems':candidates})[:12]:
        conflicts=[]
        for q in members:
            if p.get('instance') and q.get('instance') and (p['instance'],p.get('boot_id'))!=(q['instance'],q.get('boot_id')):
                conflicts.append('现有记录的实例/启动批次与目标不同')
            if p.get('request_id') and q.get('request_id') and p['request_id']!=q['request_id']:
                conflicts.append('现有 request_id 不同，需父子 trace/调用关联')
        text='关联尚未建立；这些记录不等于当前目标的形成原因。'
        if conflicts:text+='；'.join(dict.fromkeys(conflicts))+'；不能用作同任务的直接触发证据。'
        text+='需提供：与目标对应的 request/trace 及父子 span、实例/boot、原始事件时间和先后顺序；源故障如何传递到目标异常的连续阶段记录（实际经过的调用失败、资源等待、期限或取消分支）。'
        text+='只有同服务或共同出现不构成因果；无法关联保持未知；完整记录证明不在该路径时才可排除这条关联。'
        text+='；线索编号：'+'、'.join(q['problem_id'] for q in members)
        out.append({'label':'关联核查线索 · '+title(members),'value':text})
    if len(groups({'problems':candidates}))>12:
        out.append({'label':'关联覆盖范围','value':'本次仅展示前 12 类同范围线索；其余未检查，不能据此排除。请指定其他线索继续。'})
    return out
