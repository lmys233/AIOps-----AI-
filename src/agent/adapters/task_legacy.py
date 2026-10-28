"""Read-only one-time legacy import; original archives remain unchanged."""
from ..domain.tasks import Task
from .task_projection import project


async def import_tasks(service,scope):
    history=getattr(service,'history',None);rows=[]
    reader=getattr(getattr(history,'store',None),'model_turns',None)
    if callable(reader):rows=list(reversed(await reader(scope.session_id,scope.observed_id,limit=20)))
    else:
        reader=getattr(getattr(history,'store',None),'dialogue_context',None) or getattr(getattr(history,'store',None),'task_turns',None)
        if callable(reader):rows=await reader(scope.session_id)
    from ..observed.context import body_scope
    selected=[];seen=set()
    for row in rows:
        b=row.get('response') or row
        if b.get('session_id')!=scope.session_id or body_scope(b)!=scope.observed_id:continue
        if (b.get('slots') or {}).get('env',scope.env)!=scope.env:continue
        key=(b.get('explanation',{}).get('dialogue') or {}).get('task_id') or b.get('incident_id')
        if key in seen:continue
        seen.add(key)
        if (b.get('intent') or {}).get('name') not in {'INCIDENT_RCA','METRIC_QUERY','REMEDIATION','KNOWLEDGE_QA','CHANGE_IMPACT','OUT_OF_SCOPE'}:continue
        t=project(Task(scope=scope,original=row.get('user_message','')[:4000]),b,imported=True);t.revision=1;t.legacy_id=key
        selected.append(t)
    # Existing unfinished queries may predate durable dialogue snapshots.
    from ..api.tasks import load_parked
    session=getattr(service.orchestrator,'session',None)
    for p in await load_parked(session,scope.session_id):
        if p.get('incident_id') in {t.last_incident_id for t in selected}:continue
        b=await service.get_incident(p.get('incident_id',''))
        if not b or body_scope(b)!=scope.observed_id:continue
        t=project(Task(scope=scope,original=p['message']),b,imported=True)
        t.revision=1;t.missing=p.get('slots_missing') or t.missing
        if t.missing:t.state='awaiting_scope'
        selected.append(t)
    return selected[:16]
