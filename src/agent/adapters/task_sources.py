"""Bounded, same-task archive recovery; never fetch a missing witness upstream."""
from copy import deepcopy
from ..presentation.integrity import missing
from ..dialogue.plan_projection import merge_evidence


def same_scope(body,parent,task):
    from ..observed.context import body_scope
    if body_scope(parent)!=task.scope.observed_id or parent.get('session_id')!=task.scope.session_id:return False
    meta=(parent.get('explanation') or {}).get('dialogue') or {}
    if meta.get('task_id')!=task.task_id:return False
    a=body.get('slots') or {};b=parent.get('slots') or {}
    if any(a.get(k)!=b.get(k) for k in ('env','cluster','time_window')):return False
    ps=((body.get('explanation') or {}).get('attribution_v2') or {}).get('problems') or []
    older=((parent.get('explanation') or {}).get('attribution_v2') or {}).get('problems') or []
    by_id={p['problem_id']:p for p in older}
    fields=('service','env','cluster','time_range','first_seen','evidence_ids','fact')
    return all(p['problem_id'] in by_id and all(p.get(k)==by_id[p['problem_id']].get(k) for k in fields) for p in ps)


async def recover(service,body,task):
    if not missing(body):return body
    out=deepcopy(body);cursor=body;seen={body['incident_id']};sources=[]
    for _ in range(8):
        iid=((cursor.get('explanation') or {}).get('dialogue') or {}).get('based_on_incident')
        if not iid or iid in seen:break
        seen.add(iid);parent=await service.get_incident(iid)
        if not parent or not same_scope(body,parent,task):break
        out['evidence']=merge_evidence(out.get('evidence') or [],parent.get('evidence') or [])
        sources.append(iid);cursor=parent
        if not missing(out):break
    if sources:out.setdefault('explanation',{})['witness_recovery']={'source':'same_task_saved_incidents','incident_ids':sources,'upstream_queries':0}
    return out
