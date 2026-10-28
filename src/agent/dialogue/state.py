"""Derive versions from existing chat archives, never a new authority store."""
from copy import deepcopy
import re
from .contracts import metadata,snapshot,task_id

async def candidates(service,sid,target=''):
    history=getattr(service,'history',None)
    if history and hasattr(history.store,'task_turns'):
        rows=await history.store.task_turns(sid,target)
    else:
        metas=getattr(service.sessions,'turn_meta',lambda *a,**k:[])(sid,limit=20);rows=[]
        for row in reversed(list(metas)):
            body=await service.get_incident(row.get('incident_id',''))
            if body:rows.append(body)
    selected=[];seen=set()
    for body in rows:
        from ..observed.context import current,body_scope
        scope=current()
        if scope and body_scope(body)!=scope.observed_id:continue
        if body.get('session_id')!=sid or not snapshot(body).get('problems'):continue
        key=task_id(body)
        if target and target not in (key,body.get('incident_id')):continue
        if key not in seen:selected.append(body);seen.add(key)
    return selected[:8]

def bind(rows,request,decision,names):
    target=request.context.dialogue
    if target:
        for key in ('task_id','problem_id','candidate_id','requirement_key','revision'):
            if getattr(target,key):setattr(decision,key,getattr(target,key))
    text=request.message;ids=re.findall(r'\b(?:inc_[a-zA-Z0-9]+|P-[a-zA-Z0-9]+|H-[a-zA-Z0-9]+)\b',text)
    incident=next((i for i in ids if i.startswith('inc_')),'')
    if incident:decision.task_id=incident
    alias=next((b for b in rows if decision.task_id in metadata(b).get('task_aliases',[])),None)
    if alias:decision.task_id=task_id(alias)
    decision.problem_id=next((i for i in ids if i.startswith('P-')),decision.problem_id)
    broad=decision.action=='explain_gaps' and re.search('所有|全部|各个|各项|每个|其他候选|各候选',text)
    decision.candidate_id='' if broad else next((i for i in ids if i.startswith('H-')),decision.candidate_id)
    if decision.action=='explain_gaps' and not (target and target.candidate_id) and not any(i.startswith('H-') for i in ids):
        decision.candidate_id=''
    if broad:decision.requirement_key=''
    if len(set(i for i in ids if i.startswith('P-')))>1:
        from .relations import linked_target
        chosen=linked_target(rows,request,decision,[i for i in ids if i.startswith('P-')])
        if not chosen:return None,None,'请说明哪个是待解释目标、哪个是关联线索；当前不会同时核查多个问题。'
        decision.problem_id=chosen
    from .focus import prefer
    prefer(rows,request,decision,names)
    scoped=[b for b in rows if not decision.task_id or decision.task_id in (task_id(b),b['incident_id'])]
    pairs=_pairs(scoped,request,decision,names)
    if len(pairs)!=1:return None,None,'请指定服务或问题；'+ '；'.join(p['service']+' '+p['error_type']+' '+p['problem_id'] for _,p in pairs[:5])
    body,p=pairs[0];revision=metadata(body).get('revision') or body['incident_id']
    window=re.search(r'[（(]时间窗\s*(\d+(?:\.\d+)?\s*(?:[smhd]|秒|分钟|小时|天))[）)]\s*$',text,re.I)
    from ..intent.slots import normalize_time_window
    if window and normalize_time_window(window[1])!=(body.get('slots') or {}).get('time_window'):
        return None,None,'时间窗与历史问题不一致；请保留原窗口，或明确重新排查。'
    from ..attribution.v2.catalog import inventory
    cs=[x['candidate'] for x in inventory(p,snapshot(body)) if not decision.candidate_id or x['candidate']['candidate_id']==decision.candidate_id]
    if decision.candidate_id and not cs:return None,None,'该候选不属于当前问题。'
    if decision.requirement_key and not any(r['key']==decision.requirement_key for c in cs for r in c['requirements']):
        return None,None,'该验证条件不属于当前问题。'
    if decision.revision and decision.revision!=revision:return None,None,'任务版本已变化，请基于最新记录确认。'
    if request.context.env and request.context.env!=p['env']:return None,None,'环境与历史问题不一致，请明确开启新任务。'
    if request.context.cluster and request.context.cluster!=p.get('cluster',''):return None,None,'集群与历史问题不一致，请明确开启新任务。'
    decision.task_id=task_id(body);decision.problem_id=p['problem_id'];decision.revision=revision
    return deepcopy(body),deepcopy(p),''


def _pairs(scoped,request,decision,names):
    from .focus import described,matches
    text=request.message
    explicit=[n for n in names if re.search(r'(?<![\w.-])'+re.escape(n)+r'(?![\w.-])',text)]
    descriptor=described(text) and not (decision.problem_id or decision.candidate_id)
    pairs=[];seen=set()
    for b in scoped:
        ps=snapshot(b)['problems']
        if descriptor:ps=matches(ps,text)
        for p in ps:
            if explicit and p['service'] not in explicit:continue
            if decision.problem_id and p['problem_id']!=decision.problem_id:continue
            if decision.candidate_id:
                from ..attribution.v2.catalog import inventory
                if not any(x['candidate']['candidate_id']==decision.candidate_id for x in inventory(p,snapshot(b))):continue
            if p['problem_id'] not in seen:pairs.append((b,p));seen.add(p['problem_id'])
    return pairs
