"""Explicit new attempts retain their parent's recorded cumulative cost."""
from .state import candidates
from .contracts import metadata,cost,task_id,snapshot
from .routing import quick

async def annotate(service,request,body):
    decision=quick(request.message)
    if not decision or decision.action!='new_task' or not snapshot(body).get('problems'):return
    try:rows=await candidates(service,request.session_id)
    except Exception:
        body.setdefault('degraded',[]).append('新尝试的父任务档案不可读；本轮仍受原请求预算约束。');return
    names=set(body.get('slots',{}).get('service_names') or [])
    parent=next((b for b in rows if set(b.get('slots',{}).get('service_names') or [])==names
        and b.get('slots',{}).get('env')==body.get('slots',{}).get('env')),None)
    if not parent:return
    previous=metadata(parent).get('lineage_used') or metadata(parent).get('task_used') or cost(parent)
    body['explanation']['dialogue']={'task_id':body['incident_id'],'action':'new_task','revision':body['incident_id'],
        'based_on_incident':parent['incident_id'],'parent_task_id':task_id(parent),'parent_used':previous,
        'attempt':metadata(parent).get('attempt',1)+1,'completed':True,
        'reason':'用户明确开启新的受限取证尝试，父任务累计成本保留；解释和改措辞不触发此分支。'}
