"""PG authority. Start claims persist before execution; final task/history commit together."""
from ..common.migrations import install
import asyncio,json
from ..domain.tasks import Task,Conflict
from .task_schema import DDL


def decode(row):return Task.model_validate(json.loads(row['payload']) if isinstance(row['payload'],str) else row['payload'])


async def owned(c,scope):
    from ..history.store import HistoryDeleted
    await c.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',scope.tenant+':'+scope.session_id)
    if await c.fetchval('SELECT 1 FROM agent_chat_deleted_sessions WHERE tenant_id=$1 AND session_id=$2',scope.tenant,scope.session_id):
        raise HistoryDeleted('会话已删除')
    if await c.fetchval("SELECT to_regclass('agent_system_session_views')"):
        if await c.fetchval('SELECT deleted FROM agent_system_session_views WHERE tenant=$1 AND observed_id=$2 AND session_id=$3',scope.tenant,scope.observed_id,scope.session_id):
            raise HistoryDeleted('系统会话已删除')
    if await c.fetchval("SELECT to_regclass('agent_system_sessions')"):
        oid=await c.fetchval('SELECT observed_id FROM agent_system_sessions WHERE tenant=$1 AND session_id=$2',scope.tenant,scope.session_id)
        if oid and oid!=scope.observed_id:raise PermissionError('会话属于另一个系统')


class PgTasks:
    def __init__(self,history):
        self.history=history;self.ready=False;self.lock=asyncio.Lock()
        from ..execution.store import PgExecutions
        self.executions=PgExecutions(history)
    async def pool(self):
        pool=await self.history.connection()
        async with self.lock:
            if not self.ready:
                await install(pool,'adapters/task_repository.py',DDL)
                self.ready=True
        await self.executions.pool()
        return pool
    async def load(self,scope):
        pool=await self.pool()
        async with pool.acquire() as c,c.transaction():
            await owned(c,scope)
            await self.executions.reconcile_using(c,scope)
            rows=await c.fetch('SELECT payload FROM agent_tasks WHERE tenant=$1 AND observed_id=$2 AND session_id=$3 ORDER BY updated_at DESC LIMIT 32',scope.tenant,scope.observed_id,scope.session_id)
            meta=await c.fetchrow('SELECT * FROM agent_task_sessions WHERE tenant=$1 AND observed_id=$2 AND session_id=$3',scope.tenant,scope.observed_id,scope.session_id)
        return [decode(r) for r in rows],dict(meta) if meta else {'foreground':'','revision':0,'imported':False}
    async def get(self,scope,reference):
        if not reference:return None
        pool=await self.pool()
        async with pool.acquire() as c,c.transaction():
            await owned(c,scope)
            await self.executions.reconcile_using(c,scope)
            row=await c.fetchrow("SELECT payload FROM agent_tasks WHERE tenant=$1 AND observed_id=$2 AND session_id=$3 AND (task_id=$4 OR payload->>'last_incident_id'=$4 OR payload->>'legacy_id'=$4) ORDER BY updated_at DESC LIMIT 1",scope.tenant,scope.observed_id,scope.session_id,reference)
        return decode(row) if row else None
    async def write(self,c,task,expected,event):
        raw=task.model_dump_json()
        if await c.fetchval('SELECT 1 FROM agent_task_events WHERE tenant=$1 AND observed_id=$2 AND event_id=$3',task.scope.tenant,task.scope.observed_id,event['event_id']):
            raise Conflict('同轮事件已记录，未重复执行')
        if len(raw.encode())>128000:raise ValueError('任务快照超过 128KB；原文应保存在档案')
        if expected==0:
            got=await c.fetchval('INSERT INTO agent_tasks VALUES($1,$2,$3,$4,$5,$6::jsonb,now()) ON CONFLICT DO NOTHING RETURNING revision',task.scope.tenant,task.scope.observed_id,task.scope.session_id,task.task_id,task.revision,raw)
        else:
            got=await c.fetchval('UPDATE agent_tasks SET revision=$5,payload=$6::jsonb,updated_at=now() WHERE tenant=$1 AND observed_id=$2 AND session_id=$3 AND task_id=$4 AND revision=$7 RETURNING revision',task.scope.tenant,task.scope.observed_id,task.scope.session_id,task.task_id,task.revision,raw,expected)
        if got is None:raise Conflict('任务版本冲突，未覆盖新状态')
        await c.execute('INSERT INTO agent_task_events VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,now())',task.scope.tenant,task.scope.observed_id,task.scope.session_id,task.task_id,event['event_id'],task.revision,json.dumps(event))
    async def start(self,task,expected,session_revision,event,*,execution=None):
        pool=await self.pool();s=task.scope
        async with pool.acquire() as c,c.transaction():
            await owned(c,s)
            if execution:
                if execution.store is not self.executions or not execution.scope.same(task.scope) or execution.task_id!=task.task_id or execution.rid!=task.execution_id or execution.owner!=task.run_id:
                    raise Conflict('执行与任务登记范围不一致')
                await self.executions.claim_using(c,s,task.task_id,execution.rid,execution.owner,
                    None if execution.resuming else execution.body)
            await c.execute('INSERT INTO agent_task_sessions(tenant,observed_id,session_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING',s.tenant,s.observed_id,s.session_id)
            previous=await c.fetchval('SELECT foreground FROM agent_task_sessions WHERE tenant=$1 AND observed_id=$2 AND session_id=$3',s.tenant,s.observed_id,s.session_id)
            got=await c.fetchval('UPDATE agent_task_sessions SET foreground=$4,revision=revision+1,imported=true WHERE tenant=$1 AND observed_id=$2 AND session_id=$3 AND revision=$5 RETURNING revision',s.tenant,s.observed_id,s.session_id,task.task_id,session_revision)
            if got is None:raise Conflict('前台任务已变化，本轮未执行')
            if event['action']=='new_task' and previous and previous!=task.task_id:
                row=await c.fetchrow('SELECT payload FROM agent_tasks WHERE tenant=$1 AND observed_id=$2 AND task_id=$3',s.tenant,s.observed_id,previous)
                old=decode(row) if row else None
                if old and old.state not in ('completed','failed'):
                    parked=old.model_copy(deep=True);parked.state='suspended';parked.revision+=1
                    parked.budget_unknown=old.budget_unknown or old.state=='investigating'
                    await self.write(c,parked,old.revision,dict(event_id=event['event_id']+':suspend',action='suspend'))
            await self.write(c,task,expected,event)
    async def finish(self,task,expected,event,payload):
        pool=await self.pool()
        async with pool.acquire() as c,c.transaction():
            await owned(c,task.scope)
            await self.write(c,task,expected,event)
            number=await self.history.save_using(c,task.scope.session_id,task.run_id,payload)
            execution=(payload.get('response') or {}).get('explanation',{}).get('execution') or {}
            if execution and not execution.get('interrupted'):
                if execution.get('execution_id')!=task.execution_id:raise Conflict('结果不属于当前执行')
                await self.executions.complete_using(c,task.scope,task.execution_id,task.run_id)
            return number
    async def import_once(self,scope,tasks):
        pool=await self.pool()
        async with pool.acquire() as c,c.transaction():
            await owned(c,scope)
            await c.execute('INSERT INTO agent_task_sessions(tenant,observed_id,session_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING',scope.tenant,scope.observed_id,scope.session_id)
            row=await c.fetchrow('SELECT imported FROM agent_task_sessions WHERE tenant=$1 AND observed_id=$2 AND session_id=$3 FOR UPDATE',scope.tenant,scope.observed_id,scope.session_id)
            if row['imported']:return
            for task in reversed(tasks):
                await self.write(c,task,0,{'event_id':'import:'+task.task_id,'action':'import_legacy','source_incident':task.last_incident_id})
            await c.execute('UPDATE agent_task_sessions SET imported=true,foreground=$4 WHERE tenant=$1 AND observed_id=$2 AND session_id=$3',scope.tenant,scope.observed_id,scope.session_id,tasks[0].task_id if tasks else '')
