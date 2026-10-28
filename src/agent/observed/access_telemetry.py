"""Transactional export of real SDK access witnesses; delivery failures never become SDK failures."""
import json,os
from datetime import datetime

DDL='''CREATE TABLE IF NOT EXISTS agent_sdk_access_outbox(
 seq bigserial PRIMARY KEY,tenant text NOT NULL,observed_id text NOT NULL,attempt_id text NOT NULL,
 destination text NOT NULL,payload jsonb NOT NULL,state text NOT NULL DEFAULT 'pending',
 attempts integer NOT NULL DEFAULT 0,error text NOT NULL DEFAULT '',
 next_attempt timestamptz NOT NULL DEFAULT now(),delivered_at timestamptz,
 UNIQUE(tenant,observed_id,attempt_id,destination));
 CREATE INDEX IF NOT EXISTS sdk_access_pending ON agent_sdk_access_outbox(state,next_attempt,seq);
 CREATE TABLE IF NOT EXISTS agent_sdk_access_series(series_key text PRIMARY KEY,total bigint NOT NULL,
 last_ms bigint NOT NULL);'''


def labels(row):
    i=row['identity']
    return dict(tenant=i['tenant'],observed_id=i['observed_id'],service=i['service'],env=i['env'],
        instance=i['instance'],interface=row['interface_id'],observer=row['observer_id'],source='aiops_sdk_access')


async def enqueue(c,row):
    if row['transport']!='sdk_direct' or not row['sent']:return
    i=row['identity'];stamp=int(datetime.fromisoformat(row['finished_at']).timestamp()*1000)
    tags=labels(row);key=json.dumps(tags,sort_keys=True)
    await c.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',key)
    old=await c.fetchrow('SELECT * FROM agent_sdk_access_series WHERE series_key=$1 FOR UPDATE',key)
    total=(old['total'] if old else 0)+1
    metrics=[dict(labels=dict(tags,__name__=name),samples=[(stamp,value)]) for name,value in (
        ('aiops_sdk_access_success',int(row['ok'])),('aiops_sdk_access_duration_seconds',row['elapsed_ms']/1000),
        ('aiops_sdk_access_attempts_total',total),('aiops_sdk_access_last_attempt_timestamp_seconds',stamp/1000))]
    metrics.append(dict(labels=dict(tags,__name__='aiops_sdk_access_error',error=row['error']),samples=[(stamp,int(not row['ok']))]))
    state='rejected' if old and stamp<=old['last_ms'] else 'pending'
    if state=='pending':await c.execute('''INSERT INTO agent_sdk_access_series VALUES($1,$2,$3)
        ON CONFLICT(series_key) DO UPDATE SET total=$2,last_ms=$3''',key,total,stamp)
    for dest,payload,status in [('loki',row,'pending'),('prometheus',metrics,state)]:
        pending=await c.fetchval("SELECT count(*) FROM agent_sdk_access_outbox WHERE state='pending'")
        await c.execute('''INSERT INTO agent_sdk_access_outbox(tenant,observed_id,attempt_id,destination,payload,state,error)
            VALUES($1,$2,$3,$4,$5::jsonb,$6,$7) ON CONFLICT DO NOTHING''',i['tenant'],i['observed_id'],row['attempt_id'],
            dest,json.dumps(payload), 'rejected' if pending>=1000 else status,
            'queue_capacity' if pending>=1000 else 'timestamp_not_newer' if status=='rejected' else '')
    await c.execute('UPDATE agent_observed_systems SET revision=revision+1 WHERE tenant=$1 AND observed_id=$2',i['tenant'],i['observed_id'])


def loki_payload(row):
    tags=labels(row);tags.update(tool=row['tool'],error=row['error'])
    at=int(datetime.fromisoformat(row['finished_at']).timestamp()*1e9)
    return {'streams':[{'stream':tags,'values':[[str(at),json.dumps(row,ensure_ascii=False)]]}]}


async def deliver(client,record):
    from .remote_write import encode
    payload=json.loads(record['payload']) if isinstance(record['payload'],str) else record['payload']
    if record['destination']=='loki':
        url=os.getenv('AIOPS_LOKI_WRITE_URL') or os.getenv('AIOPS_LOKI_URL','http://127.0.0.1:3100').rstrip('/')+'/loki/api/v1/push'
        return await client.post(url,json=loki_payload(payload))
    url=os.getenv('AIOPS_PROMETHEUS_WRITE_URL') or os.getenv('AIOPS_PROMETHEUS_URL','http://127.0.0.1:9090').rstrip('/')+'/api/v1/write'
    samples=[(r['labels'],v,t) for r in payload for t,v in r['samples']]
    return await client.post(url,content=encode(samples),headers={'Content-Type':'application/x-protobuf',
        'Content-Encoding':'snappy','X-Prometheus-Remote-Write-Version':'0.1.0'})


async def flush(store,limit=20):
    import httpx
    pool=await store.connect();report={'delivered':0,'failed':0}
    async with pool.acquire() as c:
        async with c.transaction():
            # All workers deliver FIFO; a later sample cannot overtake an earlier retry on the same series.
            if not await c.fetchval('SELECT pg_try_advisory_xact_lock(620261010011)'):return report
            rows=await c.fetch("SELECT * FROM agent_sdk_access_outbox WHERE state='pending' ORDER BY seq FOR UPDATE LIMIT $1",limit)
            async with httpx.AsyncClient(timeout=2,trust_env=False) as client:
                for r in rows:
                    if r['next_attempt']>datetime.now(r['next_attempt'].tzinfo):break
                    try:response=await deliver(client,r);response.raise_for_status()
                    except Exception as exc:
                        status=getattr(getattr(exc,'response',None),'status_code',0)
                        terminal=(400<=status<500 and status!=429) or r['attempts']>=9
                        await c.execute("UPDATE agent_sdk_access_outbox SET state=$2,attempts=attempts+1,error=$3,next_attempt=now()+interval '5 seconds' WHERE seq=$1",
                            r['seq'],'rejected' if terminal else 'pending',type(exc).__name__)
                        report['failed']+=1;break
                    else:
                        await c.execute("UPDATE agent_sdk_access_outbox SET state='delivered',attempts=attempts+1,delivered_at=now() WHERE seq=$1",r['seq'])
                        report['delivered']+=1
            await c.execute("DELETE FROM agent_sdk_access_outbox WHERE state='delivered' AND delivered_at<now()-interval '7 days'")
    return report
