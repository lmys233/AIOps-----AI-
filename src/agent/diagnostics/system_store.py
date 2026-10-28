"""New scoped observations live beside immutable legacy tables, never in their PK space."""
import json
from diagnostics.contract import timestamp,validate_event
from diagnostics.observed import system_id
from ..observed.store import RegistryStore

DDL='''CREATE TABLE IF NOT EXISTS agent_system_diagnostic_events(
seq bigserial UNIQUE,tenant text NOT NULL,observed_id text NOT NULL,event_id text NOT NULL,
service text NOT NULL,env text NOT NULL,cluster text,instance text NOT NULL,boot_id text NOT NULL,
kind text NOT NULL,source text NOT NULL,occurred_at timestamptz NOT NULL,
coverage_start timestamptz NOT NULL,coverage_end timestamptz NOT NULL,
received_at timestamptz NOT NULL DEFAULT now(),payload jsonb NOT NULL,
PRIMARY KEY(tenant,observed_id,event_id));
CREATE INDEX IF NOT EXISTS system_diagnostic_scope ON agent_system_diagnostic_events
(tenant,observed_id,service,env,occurred_at DESC);
CREATE INDEX IF NOT EXISTS system_diagnostic_request ON agent_system_diagnostic_events
(tenant,observed_id,(payload#>>'{correlation,request_id}'),occurred_at DESC);
CREATE INDEX IF NOT EXISTS system_diagnostic_trace ON agent_system_diagnostic_events
(tenant,observed_id,(payload#>>'{correlation,trace_id}'),occurred_at DESC);'''


class SystemDiagnosticStore(RegistryStore):
    from ..observed.telemetry_outbox import DDL as RELAY_DDL
    ddl=DDL+RELAY_DDL

    async def save(self,rows):
        for row in rows:
            validate_event(row)
            if row['schema_version']!=2:raise ValueError('scoped archive requires version 2')
        pool=await self.connect();accepted=[]
        async with pool.acquire() as c:
            async with c.transaction():
                for row in rows:
                    i=row['identity'];body=json.dumps(row,ensure_ascii=False)
                    old=await c.fetchval('SELECT payload FROM agent_system_diagnostic_events WHERE tenant=$1 AND observed_id=$2 AND event_id=$3',i['tenant'],i['observed_id'],row['event_id'])
                    if old is not None and json.loads(old)!=row:raise ValueError('conflicting system event id')
                    await c.execute('''INSERT INTO agent_system_diagnostic_events
                        (tenant,observed_id,event_id,service,env,cluster,instance,boot_id,kind,source,occurred_at,coverage_start,coverage_end,payload)
                        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb) ON CONFLICT DO NOTHING''',
                        i['tenant'],i['observed_id'],row['event_id'],i['service'],i['env'],i['cluster'],i['instance'],i['boot_id'],
                        row['kind'],row['source'],timestamp(row['occurred_at']),timestamp(row['coverage']['start']),timestamp(row['coverage']['end']),body)
                    saved=await c.fetchval('SELECT payload FROM agent_system_diagnostic_events WHERE tenant=$1 AND observed_id=$2 AND event_id=$3',i['tenant'],i['observed_id'],row['event_id'])
                    if json.loads(saved)!=row:raise ValueError('conflicting concurrent scoped event')
                    from ..observed.telemetry_outbox import enqueue
                    await enqueue(c,row)
                    accepted.append(row['event_id'])
        return accepted

    async def query(self,tenant,service,start,end,*,observed_id,env,cluster=None,kinds=None,
                    instance=None,before=None,limit=100,faults_only=False,request_id=None,trace_id=None):
        system_id(observed_id);start,end=timestamp(start),timestamp(end)
        if end<start:raise ValueError('inverted window')
        seq=None
        if before:
            prefix=observed_id+':'+service+':'
            if not isinstance(before,str) or not before.startswith(prefix):raise PermissionError('cursor scope mismatch')
            seq=int(before[len(prefix):])
        limit=max(1,min(200,limit));pool=await self.connect()
        sql='''SELECT seq,payload,received_at FROM agent_system_diagnostic_events
            WHERE tenant=$1 AND observed_id=$2 AND service=$3 AND env=$4
            AND ($5::text IS NULL OR cluster=$5) AND (occurred_at BETWEEN $6 AND $7 OR
                kind IN ('db_migration','db_ddl','db_plan','verified_conditions') AND coverage_start<=$7 AND coverage_end>=$6)
            AND ($8::text[] IS NULL OR kind=ANY($8)) AND ($9::text IS NULL OR instance=$9)
            AND ($10::bigint IS NULL OR seq<$10)
            AND ($11::text IS NULL OR payload#>>'{correlation,request_id}'=$11)
            AND ($12::text IS NULL OR payload#>>'{correlation,trace_id}'=$12)
            AND (NOT $13::boolean OR kind ~ '(error|timeout|reject|exit|failed)' OR
                payload#>>'{payload,outcome}' IN ('exception','server_error','cancelled','timeout') OR
                kind='sql_query' AND (payload#>>'{payload,execution_ms}')::float>500)
            ORDER BY seq DESC LIMIT $14'''
        async with pool.acquire() as c:
            rows=await c.fetch(sql,tenant,observed_id,service,env,cluster,start,end,kinds,instance,seq,request_id,trace_id,faults_only,limit+1)
        selected=rows[:limit]
        return {'events':[json.loads(r['payload']) for r in selected],'has_more':len(rows)>limit,
            'next_cursor':observed_id+':'+service+':'+str(selected[-1]['seq']) if len(rows)>limit else None,
            'requested_window':[start.isoformat(),end.isoformat()],'complete':False,
            'latest_received_at':max((r['received_at'].isoformat() for r in selected),default=None),
            'reason':'Registered scoped retained observations; absence does not prove healthy coverage'}
