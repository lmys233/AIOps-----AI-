"""Durable, scoped tool attempts; bounded disk spool when PostgreSQL is unavailable."""
import asyncio,json,os
from pathlib import Path
from .store import RegistryStore
from diagnostics.access import validate_attempt

DDL='''CREATE TABLE IF NOT EXISTS agent_tool_attempts(
 tenant text NOT NULL,observed_id text NOT NULL,attempt_id text NOT NULL,
 call_id text NOT NULL,incident_id text NOT NULL,service text NOT NULL,
 transport text NOT NULL,finished_at timestamptz NOT NULL,body jsonb NOT NULL,
 original jsonb NOT NULL,PRIMARY KEY(tenant,observed_id,attempt_id));
 CREATE INDEX IF NOT EXISTS tool_attempt_scope_time ON agent_tool_attempts(tenant,observed_id,finished_at DESC);
 CREATE INDEX IF NOT EXISTS tool_attempt_incident ON agent_tool_attempts(tenant,observed_id,incident_id);
 CREATE TABLE IF NOT EXISTS agent_tool_calls(
 tenant text NOT NULL,observed_id text NOT NULL,call_id text NOT NULL,
 incident_id text NOT NULL,body jsonb NOT NULL,PRIMARY KEY(tenant,observed_id,call_id));'''


class AttemptStore(RegistryStore):
    from .access_telemetry import DDL as EXPORT_DDL
    ddl=DDL+EXPORT_DDL
    def __init__(self,dsn,spool=None):
        super().__init__(dsn)
        self.spool=Path(spool or os.getenv('AIOPS_ATTEMPT_SPOOL','.run/tool-attempt-spool'))
        self.dropped=0

    async def save(self,row,original=None):
        validate_attempt(row);item={'row':row,'original':original or {}}
        try:
            await asyncio.wait_for(self.insert(item),2)
            return 'persisted'
        except Exception:
            return await asyncio.to_thread(self.queue,item)

    async def insert(self,item):
        r=item['row'];i=r['identity'];pool=await self.connect()
        async with pool.acquire() as c:
            async with c.transaction():
                saved=await c.fetchval('''INSERT INTO agent_tool_attempts VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb)
                    ON CONFLICT DO NOTHING RETURNING 1''',i['tenant'],i['observed_id'],r['attempt_id'],r['call_id'],
                    r['incident_id'],i['service'],r['transport'],__import__('datetime').datetime.fromisoformat(r['finished_at']),
                    json.dumps(r,ensure_ascii=False),json.dumps(item['original'],ensure_ascii=False))
                if saved:
                    from .access_telemetry import enqueue
                    await enqueue(c,r)

    def queue(self,item):
        self.spool.mkdir(parents=True,exist_ok=True)
        files=list(self.spool.glob('*.json'))
        if len(files)>=1000 or sum(f.stat().st_size for f in files)>=104857600:
            self.dropped+=1;return 'dropped_capacity'
        key=item['row']['attempt_id'] if 'row' in item else item['call_id']
        target=self.spool/(key+'.json')
        temp=target.with_suffix('.tmp');temp.write_text(json.dumps(item,ensure_ascii=False),encoding='utf-8')
        temp.replace(target);return 'queued_local'

    async def replay(self):
        for file in sorted(self.spool.glob('*.json'))[:20]:
            item=json.loads(await asyncio.to_thread(file.read_text,encoding='utf-8'))
            if 'row' in item:validate_attempt(item['row']);await self.insert(item)
            else:await self.terminal(item['tenant'],item['observed_id'],item['call_id'],item['incident_id'],item['body'])
            await asyncio.to_thread(file.unlink)

    async def terminal(self,tenant,oid,cid,iid,body):
        pool=await self.connect()
        async with pool.acquire() as c:
            await c.execute('INSERT INTO agent_tool_calls VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT DO NOTHING',
                tenant,oid,cid,iid,json.dumps(body,ensure_ascii=False))

    async def finish(self,tenant,oid,cid,iid,body):
        try:await asyncio.wait_for(self.terminal(tenant,oid,cid,iid,body),2);return 'persisted'
        except Exception:return await asyncio.to_thread(self.queue,{'tenant':tenant,'observed_id':oid,
            'call_id':cid,'incident_id':iid,'body':body})

    async def page(self,tenant,oid,before=None,limit=20,service=None,start=None,end=None,transport=None,sent=None):
        limit=min(max(int(limit),1),50)
        rows=await self.fetch('''SELECT body FROM agent_tool_attempts WHERE tenant=$1 AND observed_id=$2
            AND ($3::text IS NULL OR attempt_id<$3) AND ($4::text IS NULL OR service=$4)
            AND ($5::timestamptz IS NULL OR finished_at>=$5) AND ($6::timestamptz IS NULL OR finished_at<=$6)
            AND ($8::text IS NULL OR transport=$8) AND ($9::boolean IS NULL OR (body->>'sent')::boolean=$9)
            ORDER BY attempt_id DESC LIMIT $7''',tenant,oid,before,service,start,end,min(max(limit,1),50)+1,transport,sent)
        bodies=[json.loads(r['body']) if isinstance(r['body'],str) else r['body'] for r in rows]
        return {'attempts':bodies[:limit],'has_more':len(bodies)>limit,
            'next_cursor':bodies[limit-1]['attempt_id'] if len(bodies)>limit else None}

    async def detail(self,tenant,oid,aid):
        row=await self.one('SELECT body,original FROM agent_tool_attempts WHERE tenant=$1 AND observed_id=$2 AND attempt_id=$3',tenant,oid,aid)
        if not row:raise KeyError('attempt not found in system')
        return {k:json.loads(v) if isinstance(v,str) else v for k,v in row.items()}
