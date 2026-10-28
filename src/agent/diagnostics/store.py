"""Platform archive. Business inspection credentials never used for storage."""
from ..common.migrations import install
from ..common.lifecycle import close_pool
import json
import asyncio
from datetime import datetime,timezone,timedelta
from diagnostics.contract import timestamp,validate_event

DDL="""CREATE TABLE IF NOT EXISTS agent_diagnostic_events(
seq BIGSERIAL UNIQUE,tenant TEXT NOT NULL,event_id TEXT NOT NULL,service TEXT NOT NULL,
instance TEXT NOT NULL,boot_id TEXT NOT NULL,env TEXT NOT NULL,cluster TEXT,kind TEXT NOT NULL,
source TEXT NOT NULL,occurred_at TIMESTAMPTZ NOT NULL,coverage_start TIMESTAMPTZ NOT NULL,
coverage_end TIMESTAMPTZ NOT NULL,received_at TIMESTAMPTZ NOT NULL DEFAULT now(),payload JSONB NOT NULL,
PRIMARY KEY(tenant,event_id));
CREATE INDEX IF NOT EXISTS diagnostic_scope_time ON agent_diagnostic_events
(tenant,service,env,occurred_at DESC);
CREATE INDEX IF NOT EXISTS diagnostic_source_time ON agent_diagnostic_events
(tenant,source,service,received_at DESC);
CREATE INDEX IF NOT EXISTS diagnostic_instance_kind ON agent_diagnostic_events
(tenant,service,env,instance,kind,occurred_at DESC);
CREATE INDEX IF NOT EXISTS diagnostic_request_scope ON agent_diagnostic_events
(tenant,service,env,(payload#>>'{correlation,request_id}'),occurred_at DESC);
CREATE INDEX IF NOT EXISTS diagnostic_object_time ON agent_diagnostic_events
(tenant,service,(payload#>>'{payload,object_name}'),occurred_at DESC);"""


class DiagnosticStore:
    def __init__(self,dsn):
        self.dsn=dsn;self.pool=None;self.lock=asyncio.Lock()

    async def connect(self):
        import asyncpg
        async with self.lock:
            if self.pool is None:
                pool=await asyncpg.create_pool(self.dsn,min_size=0,max_size=4,timeout=3,command_timeout=5)
                try:
                    await install(pool,'diagnostic-legacy',DDL)
                except Exception:
                    await close_pool(pool);raise
                self.pool=pool
        return self.pool

    async def save(self,rows):
        if rows and any(row.get('schema_version')==2 for row in rows):
            return await self.scoped().save(rows)
        for row in rows:validate_event(row)
        pool=await self.connect();accepted=[]
        async with pool.acquire() as c:
            async with c.transaction():
                for row in rows:
                    i=row['identity'];payload=json.dumps(row,ensure_ascii=False,allow_nan=False)
                    previous=await c.fetchval('SELECT payload FROM agent_diagnostic_events WHERE tenant=$1 AND event_id=$2',i['tenant'],row['event_id'])
                    if previous is not None and json.loads(previous)!=row:raise ValueError('conflicting event id')
                    await c.execute('INSERT INTO agent_diagnostic_events '
                        '(tenant,event_id,service,instance,boot_id,env,cluster,kind,source,occurred_at,coverage_start,coverage_end,payload) '
                        'VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb) ON CONFLICT DO NOTHING',
                        i['tenant'],row['event_id'],i['service'],i['instance'],i['boot_id'],i['env'],i['cluster'],
                        row['kind'],row['source'],timestamp(row['occurred_at']),timestamp(row['coverage']['start']),
                        timestamp(row['coverage']['end']),payload)
                    saved=await c.fetchval('SELECT payload FROM agent_diagnostic_events WHERE tenant=$1 AND event_id=$2',i['tenant'],row['event_id'])
                    if json.loads(saved)!=row:raise ValueError('conflicting concurrent event id')
                    accepted.append(row['event_id'])
        return accepted

    async def query(self,tenant,service,start,end,*,env,cluster=None,kinds=None,instance=None,before=None,limit=100,faults_only=False,request_id=None,observed_id=None):
        if observed_id:
            return await self.scoped().query(tenant,service,start,end,observed_id=observed_id,env=env,
                cluster=cluster,kinds=kinds,instance=instance,before=before,limit=limit,faults_only=faults_only,request_id=request_id)
        start,end=timestamp(start),timestamp(end)
        if end<start:raise ValueError('inverted window')
        limit=max(1,min(200,limit));pool=await self.connect()
        sql='''SELECT seq,payload,received_at FROM agent_diagnostic_events WHERE tenant=$1 AND service=$2
        AND env=$3 AND ($4::text IS NULL OR cluster = $4)
        AND (occurred_at BETWEEN $5 AND $6 OR
        (kind IN ('db_migration','db_ddl','db_plan','verified_conditions') AND coverage_start<=$6 AND coverage_end>=$5))
        AND ($7::text[] IS NULL OR kind=ANY($7)) AND ($8::text IS NULL OR instance=$8)
        AND ($9::bigint IS NULL OR seq<$9)
        AND ($12::text IS NULL OR payload#>>'{correlation,request_id}'=$12)
        AND (NOT $11::boolean OR kind IN ('sql_error','pool_timeout','worker_reject','process_exit','process_start_failed')
        OR kind='http_request' AND (payload#>>'{payload,outcome}' IN ('exception','server_error','cancelled','timeout')
            OR CASE WHEN jsonb_typeof(payload#>'{payload,total_ms}')='number' THEN (payload#>>'{payload,total_ms}')::float>1000 ELSE false END)
        OR kind='sql_query' AND CASE WHEN jsonb_typeof(payload#>'{payload,execution_ms}')='number'
            THEN (payload#>>'{payload,execution_ms}')::float>500 ELSE false END)
        ORDER BY seq DESC LIMIT $10'''
        async with pool.acquire() as c:
            rows=await c.fetch(sql,tenant,service,env,cluster,start,end,kinds,instance,before,limit+1,faults_only,request_id)
        selected=rows[:limit]
        return dict(events=[json.loads(r['payload']) for r in selected],has_more=len(rows)>limit,
            next_cursor=selected[-1]['seq'] if len(rows)>limit else None,
            requested_window=[start.isoformat(),end.isoformat()],complete=False,
            latest_received_at=max((r['received_at'].isoformat() for r in selected),default=None),
            retention_policy='Explicit diagnostic-table prune; chat deletion does not remove observations',
            reason='Retained observations; absence alone does not prove healthy coverage')

    async def prune(self,days=30):
        pool=await self.connect()
        async with pool.acquire() as c:
            return await c.execute('DELETE FROM agent_diagnostic_events WHERE received_at<$1',
                                   datetime.now(timezone.utc)-timedelta(days=max(1,days)))

    async def close(self):
        try:
            if getattr(self,'system_archive',None):await self.system_archive.close()
        finally:
            pool,self.pool=self.pool,None
            await close_pool(pool)

    def scoped(self):
        from .system_store import SystemDiagnosticStore
        if not getattr(self,'system_archive',None):self.system_archive=SystemDiagnosticStore(self.dsn)
        return self.system_archive
