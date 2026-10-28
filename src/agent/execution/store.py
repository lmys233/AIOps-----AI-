"""Scoped PostgreSQL execution authority; leases fence competing workers."""
from ..common.migrations import install
import asyncio, json
from .payload import reference,is_reference,verify
from ..domain.tasks import Conflict
from ..adapters.task_repository import owned

DDL = '''CREATE TABLE IF NOT EXISTS agent_execution_runs (
 tenant text NOT NULL, observed_id text NOT NULL, session_id text NOT NULL,
 task_id text NOT NULL, run_id text NOT NULL, owner text NOT NULL,
 lease_until timestamptz NOT NULL, status text NOT NULL, body jsonb NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant,observed_id,session_id,run_id));
 CREATE TABLE IF NOT EXISTS agent_execution_steps (
 tenant text NOT NULL, observed_id text NOT NULL, session_id text NOT NULL,
 run_id text NOT NULL, step_key text NOT NULL, body jsonb NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant,observed_id,session_id,run_id,step_key));
 CREATE TABLE IF NOT EXISTS agent_execution_checkpoints (
 tenant text NOT NULL, observed_id text NOT NULL, session_id text NOT NULL,
 run_id text NOT NULL, checkpoint_id text NOT NULL, body jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant,observed_id,session_id,run_id,checkpoint_id));'''


def dumps(value):
    raw=json.dumps(value,ensure_ascii=False,allow_nan=False)
    if len(raw.encode())>8_000_000:raise ValueError('执行快照超过 8MB，停止新增执行')
    return raw


def loads(value):return json.loads(value) if isinstance(value,str) else value


class PgExecutions:
    lease_seconds=90
    def __init__(self,history):self.history=history;self.ready=False;self.lock=asyncio.Lock()

    async def pool(self):
        pool=await self.history.connection()
        async with self.lock:
            if not self.ready:
                await install(pool,'execution/store.py',DDL)
                self.ready=True
        return pool

    def args(self,s,rid):return s.tenant,s.observed_id,s.session_id,rid

    async def read(self,s,rid):
        pool=await self.pool()
        async with pool.acquire() as c,c.transaction():
            await owned(c,s)
            row=await c.fetchrow('SELECT *,lease_until>now() AS busy FROM agent_execution_runs WHERE tenant=$1 AND observed_id=$2 AND session_id=$3 AND run_id=$4',*self.args(s,rid))
            if not row:return None
            return {**loads(row['body']),'status':row['status'],'busy':row['busy'],'task_id':row['task_id']}

    async def claim(self,s,task_id,rid,owner,body=None):
        pool=await self.pool()
        async with pool.acquire() as c,c.transaction():
            await owned(c,s)
            await self.claim_using(c,s,task_id,rid,owner,body)

    async def claim_using(self,c,s,task_id,rid,owner,body=None):
        """Caller holds the session lock and transaction shared with task start."""
        if body is not None:
            ok=await c.fetchval('''INSERT INTO agent_execution_runs VALUES
                    ($1,$2,$3,$4,$5,$6,now()+$7*interval '1 second','running',$8::jsonb,now())
                    ON CONFLICT DO NOTHING RETURNING 1''',s.tenant,s.observed_id,s.session_id,task_id,rid,owner,self.lease_seconds,dumps(body))
        else:
            ok=await c.fetchval('''UPDATE agent_execution_runs SET owner=$5,
                    lease_until=now()+$6*interval '1 second',status='running',updated_at=now()
                    WHERE tenant=$1 AND observed_id=$2 AND session_id=$3 AND run_id=$4
                    AND task_id=$7 AND status IN ('running','interrupted','ready')
                    AND lease_until<=now() RETURNING 1''',*self.args(s,rid),owner,self.lease_seconds,task_id)
        if not ok:raise Conflict('执行仍在运行、已完成或恢复引用无效；没有重复执行')

    async def renew(self,s,rid,owner,wall_ms):
        await self.write(s,rid,owner,wall_ms=wall_ms)

    async def write(self,s,rid,owner,body=None,status=None,step=None,*,wall_ms=None):
        pool=await self.pool()
        async with pool.acquire() as c,c.transaction():
            await owned(c,s)
            row=await c.fetchrow('''SELECT body->'memory' AS memory FROM agent_execution_runs WHERE tenant=$1 AND observed_id=$2 AND session_id=$3
                AND run_id=$4 AND owner=$5 AND lease_until>now() FOR UPDATE''',*self.args(s,rid),owner)
            if not row:raise Conflict('执行租约失效，停止写入与新增调用')
            ref=None
            if body is not None:
                data=dict(body);memory=data.get('memory')
                if memory and not is_reference(memory):
                    ref,raw=reference(memory)
                    if loads(row['memory'])!=ref:
                        await c.execute('''INSERT INTO agent_execution_checkpoints VALUES($1,$2,$3,$4,$5,$6::jsonb,now())
                            ON CONFLICT DO NOTHING''',*self.args(s,rid),ref['checkpoint_id'],dumps(raw))
                    data['memory']=ref
                elif memory:ref=memory
                raw=dumps(data)
            else:raw=None
            await c.execute('''UPDATE agent_execution_runs SET body=CASE WHEN $6::jsonb IS NOT NULL THEN $6::jsonb
                WHEN $9::double precision IS NOT NULL THEN jsonb_set(body,'{used,wall_ms}',to_jsonb($9::double precision))
                ELSE body END,status=COALESCE($7,status),
                lease_until=CASE WHEN $7 IN ('interrupted','completed') THEN now()
                ELSE now()+$8*interval '1 second' END,updated_at=now()
                WHERE tenant=$1 AND observed_id=$2 AND session_id=$3 AND run_id=$4 AND owner=$5''',
                *self.args(s,rid),owner,raw,status,self.lease_seconds,wall_ms)
            if step is not None:
                key,value=step
                await c.execute('''INSERT INTO agent_execution_steps VALUES($1,$2,$3,$4,$5,$6::jsonb,now())
                    ON CONFLICT(tenant,observed_id,session_id,run_id,step_key)
                    DO UPDATE SET body=EXCLUDED.body,updated_at=now()''',*self.args(s,rid),key,dumps(value))
            return ref

    async def checkpoint(self,s,rid,ref):
        if not is_reference(ref):
            from .checkpoint import validate
            validate(ref);return ref # read-only v1 inline migration
        pool=await self.pool()
        async with pool.acquire() as c,c.transaction():
            await owned(c,s)
            value=await c.fetchval('''SELECT body FROM agent_execution_checkpoints WHERE tenant=$1
                AND observed_id=$2 AND session_id=$3 AND run_id=$4 AND checkpoint_id=$5''',
                *self.args(s,rid),ref['checkpoint_id'])
            if value is None:raise Conflict('恢复检查点引用缺失，停止自动恢复；未重发旧调用')
            payload=loads(value)
            # Canonical JSON is stored as a JSON string, so JSONB cannot normalize
            # 1e20 to an integer or -0.0 to 0.0 and change replay parameter keys.
            # Previously retained object payloads remain readable.
            try:payload=loads(payload)
            except (ValueError,TypeError) as exc:raise ValueError('恢复检查点载荷损坏，停止恢复') from exc
            return verify(ref,payload)

    async def steps(self,s,rid):
        pool=await self.pool()
        async with pool.acquire() as c,c.transaction():
            await owned(c,s)
            rows=await c.fetch('SELECT step_key,body FROM agent_execution_steps WHERE tenant=$1 AND observed_id=$2 AND session_id=$3 AND run_id=$4 LIMIT 257',*self.args(s,rid))
            if len(rows)>256:raise Conflict('执行步骤超过恢复上限，停止自动恢复')
            return {r['step_key']:loads(r['body']) for r in rows}

    async def page(self,s,rid,after='',limit=20):
        pool=await self.pool()
        async with pool.acquire() as c,c.transaction():
            await owned(c,s)
            rows=await c.fetch('''SELECT step_key,body FROM agent_execution_steps WHERE tenant=$1 AND observed_id=$2
                AND session_id=$3 AND run_id=$4 AND step_key>$5 ORDER BY step_key LIMIT $6''',*self.args(s,rid),after,limit+1)
            return [(r['step_key'],loads(r['body'])) for r in rows]

    async def complete(self,s,rid,owner):
        pool=await self.pool()
        async with pool.acquire() as c,c.transaction():
            await owned(c,s)
            await self.complete_using(c,s,rid,owner)

    async def complete_using(self,c,s,rid,owner):
        ok=await c.fetchval("""UPDATE agent_execution_runs SET status='completed',lease_until=now(),updated_at=now()
            WHERE tenant=$1 AND observed_id=$2 AND session_id=$3 AND run_id=$4 AND owner=$5
            AND (status='completed' OR (status='ready' AND lease_until>now())) RETURNING 1""",
            *self.args(s,rid),owner)
        if not ok:raise Conflict('执行终态提交的租约或状态已变化，未提交任务与历史')

    async def reconcile_using(self,c,s):
        """Repair pre-T30 ready rows only from their authoritative committed receipt."""
        await c.execute("""UPDATE agent_execution_runs r SET status='completed',lease_until=now(),updated_at=now()
            WHERE r.tenant=$1 AND r.observed_id=$2 AND r.session_id=$3 AND r.status='ready'
            AND EXISTS(SELECT 1 FROM agent_chat_turns h JOIN agent_task_events e
              ON e.tenant=h.tenant_id AND e.observed_id=r.observed_id AND e.task_id=r.task_id
              AND e.event_id=h.session_id||':'||h.turn_id||':finish'
              AND e.event->>'phase'='finish' AND e.event->>'incident_id'=h.incident_id
              WHERE h.tenant_id=r.tenant AND h.session_id=r.session_id AND h.turn_id=r.owner
              AND h.payload#>>'{response,explanation,task_control,task_id}'=r.task_id
              AND h.payload#>>'{response,explanation,task_control,commit_state}'='committed'
              AND h.payload#>>'{response,explanation,execution,execution_id}'=r.run_id
              AND COALESCE(h.payload#>>'{response,explanation,execution,interrupted}','false')='false')""",
              s.tenant,s.observed_id,s.session_id)


class MemoryExecutions:
    """Explicit offline test authority, never a fallback for PG failures."""
    lease_seconds=90
    def __init__(self):self.runs={};self.items={};self.checkpoints={}
    def key(self,s,r):return s.tenant,s.observed_id,s.session_id,r
    async def read(self,s,r):
        from copy import deepcopy
        return deepcopy(self.runs.get(self.key(s,r)))
    async def claim(self,s,task_id,rid,owner,body=None):
        key=self.key(s,rid);old=self.runs.get(key)
        if (body is not None and old) or (body is None and (not old or old['busy'] or old['task_id']!=task_id or old['status']=='completed')):raise Conflict('执行租约冲突')
        self.runs[key]={**(body or old),'task_id':task_id,'owner':owner,'status':'running','busy':True}
    async def renew(self,s,rid,owner,wall_ms):
        await self.write(s,rid,owner,wall_ms=wall_ms)
    async def write(self,s,rid,owner,body=None,status=None,step=None,*,wall_ms=None):
        key=self.key(s,rid);old=self.runs[key]
        if not old['busy'] or old['owner']!=owner:raise Conflict('执行租约失效')
        data=loads(dumps(body if body is not None else old));memory=data.get('memory');ref=None;raw=None
        if wall_ms is not None:data['used']['wall_ms']=wall_ms
        if memory:
            if not is_reference(memory):ref,raw=reference(memory);data['memory']=ref
            else:ref=memory
        value=loads(dumps(step[1])) if step else None
        if raw is not None:self.checkpoints.setdefault((*key,ref['checkpoint_id']),loads(raw))
        self.runs[key]={**data,'task_id':old['task_id'],'owner':owner,'status':status or old['status'],'busy':status not in ('interrupted','completed')}
        if step:self.items.setdefault(key,{})[step[0]]=value
        return ref
    async def checkpoint(self,s,rid,ref):
        from copy import deepcopy
        if not is_reference(ref):
            from .checkpoint import validate
            validate(ref);return deepcopy(ref)
        value=self.checkpoints.get((*self.key(s,rid),ref['checkpoint_id']))
        if value is None:raise Conflict('恢复检查点引用缺失，停止自动恢复；未重发旧调用')
        return verify(ref,deepcopy(value))
    async def steps(self,s,rid):return loads(dumps(self.items.get(self.key(s,rid),{})))
    async def page(self,s,rid,after='',limit=20):
        return [(k,v) for k,v in sorted((await self.steps(s,rid)).items()) if k>after][:limit+1]
    async def complete(self,s,rid,owner):
        row=self.runs[self.key(s,rid)]
        if row['owner']!=owner or row['status'] not in ('ready','completed') or (row['status']=='ready' and not row['busy']):
            raise Conflict('执行终态提交的租约或状态已变化')
        row.update(status='completed',busy=False)
