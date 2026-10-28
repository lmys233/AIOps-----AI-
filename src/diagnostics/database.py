"""Configured PostgreSQL snapshots; all SQL fixed, parameterized and read-only."""
import json
import os
from pathlib import Path
from .contract import event,utc_now,timestamp
from .sql import sql_shape

ACTIVITY_SQL="""SELECT pid,datname,state,xact_start,query_start,wait_event_type,
wait_event,application_name,query,pg_blocking_pids(pid) AS blocking_pids
FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
AND ($1::text IS NULL OR application_name=$1) ORDER BY pid LIMIT $2"""
LOCK_SQL="""SELECT pid,locktype,mode,granted,relation::regclass::text AS object_name,
classid::text,objid::text,objsubid,transactionid::text,pg_blocking_pids(pid) AS blocking_pids
FROM pg_locks WHERE pid=ANY($1::int[]) ORDER BY pid LIMIT $2"""
IDENTITY_SQL="""SELECT current_database() AS database,current_schema() AS schema,
current_setting('search_path') AS search_path,current_setting('transaction_read_only') AS read_only,
COALESCE(inet_server_addr()::text,'local') AS server_address,inet_server_port() AS server_port"""


class SourceMissing(ValueError):pass


class PostgresObserver:
    def __init__(self,mapping,identity):
        self.mapping=mapping;self.identity=identity
        if 'observed_id' in identity:
            from .observed import scoped_identity
            scoped_identity(identity)
            if not mapping.get('expected_database') or not (mapping.get('application_name') or mapping.get('exclusive_database')):
                raise SourceMissing('scoped observer requires verified database and application/exclusive binding')

    async def read(self,kind,limit=200):
        import asyncpg
        if self.mapping.get('driver','postgres')!='postgres':raise SourceMissing('unsupported driver')
        dsn=os.getenv(self.mapping.get('dsn_env',''))
        if not dsn:raise SourceMissing('inspection DSN not configured')
        conn=await asyncpg.connect(dsn,timeout=3,command_timeout=3)
        try:
            async with conn.transaction(readonly=True):
                from .identity import database_identity
                actual=database_identity(await conn.fetchrow(IDENTITY_SQL))
                if actual['read_only']!='on':raise SourceMissing('read-only transaction not active')
                if self.mapping.get('expected_database') and actual['database']!=self.mapping['expected_database']:
                    raise SourceMissing('inspection database scope mismatch')
                if self.mapping.get('expected_schema') and actual['schema']!=self.mapping['expected_schema']:
                    raise SourceMissing('inspection schema scope mismatch')
                fetched=await self.snapshot(conn,kind,max(1,min(200,limit)))
                rows=[];size=0
                for row in fetched:
                    count=len(json.dumps(row,ensure_ascii=False).encode())
                    if size+count>48000:break
                    rows.append(row);size+=count
                payload=dict(actual=actual,rows=rows,server_id=self.mapping.get('server_id'),
                    service_binding='application_name' if self.mapping.get('application_name') else 'database_only')
                now=utc_now();limited=len(fetched)>=limit or len(rows)!=len(fetched)
                denied=any(r.get('permission_limited') for r in rows)
                return event(self.identity,kind,'database_observer',payload,
                    coverage=dict(start=now,end=now,complete=not (limited or denied)),
                    quality=dict(status='partial' if limited or denied else 'available',
                                 reason='Database view visibility limited; pg_read_all_stats may be required' if denied else 'Current snapshot',
                                 sampled=False,truncated=limited,dropped=0))
        finally:await conn.close()

    async def snapshot(self,conn,kind,limit):
        if kind=='db_context':return []
        activity=[normalize(dict(r)) for r in await conn.fetch(ACTIVITY_SQL,
                    self.mapping.get('application_name'),limit)]
        if kind=='db_activity':return activity
        if kind=='db_locks':
            scoped='observed_id' in self.identity and not self.mapping.get('exclusive_database')
            pids=sorted({pid for r in activity for pid in ([r['pid']] if scoped else [r['pid']]+r.get('blocking_pids',[]))})[:200]
            return [normalize(dict(r)) for r in await conn.fetch(LOCK_SQL,pids,limit)]
        if kind=='db_slow_queries':
            if 'observed_id' in self.identity and not self.mapping.get('exclusive_database'):
                raise SourceMissing('shared-database cumulative SQL statistics cannot be attributed to one system')
            present=await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_stat_statements')")
            if not present:raise SourceMissing('pg_stat_statements is not available; not enabled automatically')
            query="SELECT query,calls,total_exec_time,rows FROM pg_stat_statements WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database()) ORDER BY total_exec_time DESC LIMIT $1"
            reset=await conn.fetchval('SELECT stats_reset FROM pg_stat_statements_info')
            return [dict(normalize(dict(r)),measurement_kind='cumulative',execution_unit='milliseconds',
                stats_reset=reset.isoformat() if reset else None,service_binding='database_only')
                for r in await conn.fetch(query,limit)]
        raise SourceMissing('snapshot kind not supported')


def normalize(row):
    from datetime import datetime,timezone
    if isinstance(row.get('xact_start'),datetime):
        row['transaction_age_seconds']=max(0,(datetime.now(timezone.utc)-row['xact_start']).total_seconds())
    if 'query' in row:
        query=row.pop('query')
        row['permission_limited']=query is None or '<insufficient privilege>' in str(query)
        row.update(sql_shape(query or '<unavailable>'))
    for key,value in list(row.items()):
        if isinstance(value,datetime):row[key]=value.isoformat()
    return row


def history_file(path,identity,kind,source):
    p=Path(path)
    if p.stat().st_size>512000:raise SourceMissing('historical source exceeds limit')
    data=json.loads(p.read_text(encoding='utf-8'))
    if data.get('verification_method')!='verified_human':raise SourceMissing('source not verified')
    start,end=data['time_range'];timestamp(start);timestamp(end)
    rows=[r for r in data.get('records',[]) if r.get('service')==identity['service']]
    if not rows:raise SourceMissing('target records missing; empty is not counterevidence')
    from hashlib import sha256
    observation_id=sha256(json.dumps([identity['service'],kind,start,end,rows],sort_keys=True).encode()).hexdigest()
    return event(identity,kind,source,{'records':rows,'verification_method':'verified_human'},observation_id=observation_id,
        occurred_at=end,coverage=dict(start=start,end=end,complete=data.get('complete') is True))


def deadlock_log(record,identity):
    if record.get('sql_state_code')!='40P01':raise SourceMissing('not a PostgreSQL deadlock report')
    stamp=record['log_time'];timestamp(stamp)
    return event(identity,'db_deadlock','database_log',dict(server_pid=record.get('process_id'),
        message=str(record.get('message',''))[:12000],detail=str(record.get('detail',''))[:20000],
        **sql_shape(record.get('query',''))),occurred_at=stamp)
