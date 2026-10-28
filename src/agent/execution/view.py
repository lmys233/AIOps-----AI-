"""Read-only task memory and paged execution inspection through workspace authority."""
from ..observed.workspace import authorize,store_of
from ..adapters.task_runtime import controller,executions
from ..domain.tasks import Scope

async def page(service,oid,sid,tid,after='',limit=20):
    if not 1<=limit<=50 or len(after)>100:raise ValueError('非法执行分页参数')
    tenant,_=await authorize(service,oid);await store_of(service).owned(tenant,oid,sid)
    repo=controller(service).repository
    task=await repo.get(Scope(tenant=tenant,observed_id=oid,session_id=sid,env='prod'),tid)
    if not task:raise KeyError('任务不属于当前系统/会话')
    store=getattr(repo,'executions',None) or executions(service)
    run=await store.read(task.scope,task.execution_id) if task.execution_id else None
    if not run:return {'task_id':task.task_id,'memory':task.memory,'execution':None,'steps':[],'next_cursor':None}
    rows=await store.page(task.scope,task.execution_id,after,limit)
    steps=[{'step_key':key,**value} for key,value in rows[:limit]]
    # Semantic snapshot is deliberately separate from the typed runtime checkpoint.
    memory=run.get('task_memory') or task.memory or {'goal':task.original,'purpose':task.purpose,'slots':task.slots}
    checkpoint=run.get('memory') or {};fields=checkpoint.get('fields') or {}
    return {'task_id':task.task_id,'memory':memory,'execution':{k:run.get(k) for k in
        ('status','busy','node','started_at','used','recoveries')},
        'memory_snapshot':{'saved':bool(checkpoint),'fields_available':checkpoint.get('fields_available',list(fields))},
        'steps':steps,'next_cursor':rows[limit-1][0] if len(rows)>limit else None}
