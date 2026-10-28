"""Explicit offline authority for injected tests; production uses PG without fail-open."""
from copy import deepcopy
import asyncio
from contextlib import asynccontextmanager
from ..domain.tasks import Conflict


class MemoryTasks:
    def __init__(self):
        self.tasks={};self.sessions={};self.events={};self.bodies={}
        self.lock=asyncio.Lock()
        from ..execution.store import MemoryExecutions
        self.executions=MemoryExecutions()
    def key(self,s):return s.tenant,s.observed_id,s.session_id
    async def load(self,s):
        key=self.key(s)
        return [deepcopy(t) for t in reversed(list(self.tasks.values())) if self.key(t.scope)==key],deepcopy(self.sessions.get(key,{'foreground':'','revision':0,'imported':False}))
    async def get(self,s,reference):
        return next((deepcopy(t) for t in self.tasks.values() if self.key(t.scope)==self.key(s) and reference in (t.task_id,t.last_incident_id,t.legacy_id)),None) if reference else None
    def write(self,t,expected,event):
        old=self.tasks.get(t.task_id)
        if (old.revision if old else 0)!=expected or event['event_id'] in self.events:raise Conflict('任务版本/事件冲突')
        self.tasks[t.task_id]=deepcopy(t);self.events[event['event_id']]=deepcopy(event)
    @asynccontextmanager
    async def atomic(self):
        async with self.lock:
            saved=deepcopy((self.tasks,self.sessions,self.events,self.bodies,self.executions.runs,self.executions.items,self.executions.checkpoints))
            try:yield
            except BaseException:
                self.tasks,self.sessions,self.events,self.bodies,self.executions.runs,self.executions.items,self.executions.checkpoints=saved
                raise
    async def start(self,t,expected,srevision,event,*,execution=None):
        async with self.atomic():
            if execution:
                if execution.store is not self.executions or not execution.scope.same(t.scope) or execution.task_id!=t.task_id or execution.rid!=t.execution_id or execution.owner!=t.run_id:
                    raise Conflict('执行与任务登记范围不一致')
                await self.executions.claim(t.scope,t.task_id,execution.rid,execution.owner,
                    None if execution.resuming else execution.body)
            self.start_using(t,expected,srevision,event)
    def start_using(self,t,expected,srevision,event):
        key=self.key(t.scope);meta=self.sessions.get(key,{'revision':0})
        if meta['revision']!=srevision:raise Conflict('前台任务版本冲突')
        previous=self.tasks.get(meta.get('foreground',''))
        if previous and previous.task_id!=t.task_id and event['action']=='new_task' and previous.state not in ('completed','failed'):
            parked=previous.model_copy(deep=True);parked.state='suspended';parked.revision+=1
            parked.budget_unknown=previous.budget_unknown or previous.state=='investigating'
            self.write(parked,previous.revision,dict(event_id=event['event_id']+':suspend',action='suspend'))
        self.write(t,expected,event);self.sessions[key]={'foreground':t.task_id,'revision':srevision+1,'imported':True}
    async def finish(self,t,expected,event,payload):
        async with self.atomic():
            self.write(t,expected,event);self.bodies[payload['incident_id']]=deepcopy(payload['response'])
            execution=(payload.get('response') or {}).get('explanation',{}).get('execution') or {}
            if execution and not execution.get('interrupted'):
                if execution.get('execution_id')!=t.execution_id:raise Conflict('结果不属于当前执行')
                await self.executions.complete(t.scope,t.execution_id,t.run_id)
            return len(self.bodies)
    async def import_once(self,s,tasks):
        key=self.key(s)
        if self.sessions.get(key,{}).get('imported'):return
        for t in reversed(tasks):self.write(t,0,{'event_id':'import:'+t.task_id,'action':'import_legacy'})
        self.sessions[key]={'foreground':tasks[0].task_id if tasks else '','revision':0,'imported':True}
