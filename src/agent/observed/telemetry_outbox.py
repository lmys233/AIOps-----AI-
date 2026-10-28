"""Atomic enqueue with archive storage; transport failure is a platform delivery failure."""
import json,os
from .telemetry_payloads import loki,otlp

DDL='''CREATE TABLE IF NOT EXISTS agent_system_telemetry_outbox(
seq bigserial UNIQUE,tenant text NOT NULL,observed_id text NOT NULL,event_id text NOT NULL,
destination text NOT NULL,payload jsonb NOT NULL,state text NOT NULL DEFAULT 'pending',
attempts integer NOT NULL DEFAULT 0,error text NOT NULL DEFAULT '',
next_attempt timestamptz NOT NULL DEFAULT now(),delivered_at timestamptz,
PRIMARY KEY(tenant,observed_id,event_id,destination));
CREATE INDEX IF NOT EXISTS system_telemetry_pending ON agent_system_telemetry_outbox(state,next_attempt,seq);'''


async def enqueue(c,row):
    dest={'application_log':'loki','trace_span':'tempo'}.get(row['kind'])
    if not dest:return
    i=row['identity']
    pending=await c.fetchval("SELECT count(*) FROM agent_system_telemetry_outbox WHERE state='pending'")
    await c.execute('''INSERT INTO agent_system_telemetry_outbox(tenant,observed_id,event_id,destination,payload,state,error)
        VALUES($1,$2,$3,$4,$5::jsonb,$6,$7) ON CONFLICT DO NOTHING''',i['tenant'],i['observed_id'],row['event_id'],dest,json.dumps(row),
        'rejected' if pending>=1000 else 'pending','queue_capacity' if pending>=1000 else '')


async def flush(store,limit=32):
    import httpx
    pool=await store.connect();report={'delivered':0,'failed':0,'unconfigured':0}
    urls={'loki':os.getenv('AIOPS_LOKI_WRITE_URL') or os.getenv('AIOPS_LOKI_URL','http://127.0.0.1:3100').rstrip('/')+'/loki/api/v1/push',
        'tempo':os.getenv('AIOPS_TEMPO_INGEST_URL','http://127.0.0.1:4318/v1/traces')}
    async with pool.acquire() as c:
        async with c.transaction():
            rows=await c.fetch("SELECT * FROM agent_system_telemetry_outbox WHERE state='pending' AND next_attempt<=now() ORDER BY seq FOR UPDATE SKIP LOCKED LIMIT $1",limit)
            async with httpx.AsyncClient(timeout=2) as client:
                for record in rows:
                    row=json.loads(record['payload']);url=urls.get(record['destination'])
                    if not url:report['unconfigured']+=1;continue
                    try:
                        if record['destination']=='loki':r=await client.post(url,json=loki(row))
                        else:r=await client.post(url,content=otlp(row),headers={'Content-Type':'application/x-protobuf'})
                        r.raise_for_status()
                    except Exception as exc:
                        terminal=record['attempts']>=9 or (isinstance(exc,httpx.HTTPStatusError) and 400<=exc.response.status_code<500 and exc.response.status_code!=429)
                        await c.execute("UPDATE agent_system_telemetry_outbox SET state=$2,attempts=attempts+1,error=$3,next_attempt=now()+interval '5 seconds' WHERE seq=$1",record['seq'],'rejected' if terminal else 'pending',type(exc).__name__)
                        report['failed']+=1
                    else:
                        await c.execute("UPDATE agent_system_telemetry_outbox SET state='delivered',attempts=attempts+1,error='',delivered_at=now() WHERE seq=$1",record['seq'])
                        report['delivered']+=1
            await c.execute("DELETE FROM agent_system_telemetry_outbox WHERE state='delivered' AND delivered_at<now()-interval '7 days'")
    return report
