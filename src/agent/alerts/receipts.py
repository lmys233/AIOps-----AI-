"""Persistent reception receipt, separate from the in-process execution queue."""
import json
from uuid import uuid4
from ..observed.store import RegistryStore

DDL='''CREATE TABLE IF NOT EXISTS agent_alert_receipts(
 tenant text NOT NULL, receipt_id text NOT NULL, observed_id text NOT NULL DEFAULT '',
 fingerprint text NOT NULL, incident_id text NOT NULL DEFAULT '', session_id text NOT NULL,
 state text NOT NULL, record jsonb NOT NULL, error text NOT NULL DEFAULT '',
 received_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant,receipt_id));
 CREATE INDEX IF NOT EXISTS agent_alert_receipt_scope ON agent_alert_receipts(tenant,observed_id,received_at DESC);
 ALTER TABLE agent_alert_receipts ADD COLUMN IF NOT EXISTS sequence bigserial;
 CREATE INDEX IF NOT EXISTS agent_alert_receipt_sequence ON agent_alert_receipts(tenant,observed_id,sequence DESC);
 CREATE TABLE IF NOT EXISTS agent_warning_reviews(
 tenant text NOT NULL, observed_id text NOT NULL, env text NOT NULL, cluster text NOT NULL,
 reviewer text NOT NULL, through_sequence bigint NOT NULL CHECK(through_sequence>=0),
 updated_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(tenant,observed_id,env,cluster,reviewer));'''


class ReceptionUnavailable(Exception):pass


class PgReceipts(RegistryStore):
    ddl=DDL
    async def put_many(self,tenant,rows):
        pool=await self.connect()
        async with pool.acquire() as c,c.transaction():
            await c.executemany('''INSERT INTO agent_alert_receipts
                (tenant,receipt_id,observed_id,fingerprint,incident_id,session_id,state,record,error)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9)''',[
                (tenant,row['receipt_id'],row['observed_id'],row['fingerprint'],row['incident_id'],row['session_id'],
                 row['state'],json.dumps(row['record'],ensure_ascii=False),row.get('error','')) for row in rows])
    async def put(self,tenant,row):
        await self.fetch('''INSERT INTO agent_alert_receipts
            (tenant,receipt_id,observed_id,fingerprint,incident_id,session_id,state,record,error)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9) RETURNING receipt_id''',
            tenant,row['receipt_id'],row['observed_id'],row['fingerprint'],row['incident_id'],row['session_id'],
            row['state'],json.dumps(row['record'],ensure_ascii=False),row.get('error',''))
    async def update(self,tenant,rid,state,error=''):
        await self.fetch('UPDATE agent_alert_receipts SET state=$3,error=$4,updated_at=now() WHERE tenant=$1 AND receipt_id=$2 RETURNING 1',tenant,rid,state,error)
    async def decide(self,tenant,row):
        await self.fetch('''UPDATE agent_alert_receipts SET state=$3,incident_id=$4,updated_at=now()
            WHERE tenant=$1 AND receipt_id=$2 RETURNING 1''',tenant,row['receipt_id'],row['state'],row['incident_id'])
    async def page(self,tenant,oid):
        rows=await self.fetch('SELECT * FROM agent_alert_receipts WHERE tenant=$1 AND observed_id=$2 ORDER BY received_at DESC LIMIT 50',tenant,oid)
        for row in rows:
            if isinstance(row['record'],str):row['record']=json.loads(row['record'])
        return rows
    async def prior_incident(self,tenant,oid,fingerprint):
        row=await self.one('''SELECT incident_id FROM agent_alert_receipts
            WHERE tenant=$1 AND observed_id=$2 AND fingerprint=$3 AND incident_id<>''
            ORDER BY received_at DESC LIMIT 1''',tenant,oid,fingerprint)
        return row['incident_id'] if row else ''


class LocalReceipts:
    """Explicit single-process fallback for deployments without a history database."""
    def __init__(self):self.rows={};self.sequence=0;self.reviews={}
    async def put(self,tenant,row):
        self.sequence+=1
        self.rows[(tenant,row['receipt_id'])]=dict(row,sequence=self.sequence)
        while len(self.rows)>512:self.rows.pop(next(iter(self.rows)))
    async def put_many(self,tenant,rows):
        for row in rows:await self.put(tenant,row)
    async def update(self,tenant,rid,state,error=''):
        self.rows[(tenant,rid)].update(state=state,error=error)
    async def decide(self,tenant,row):self.rows[(tenant,row['receipt_id'])].update(row)
    async def page(self,tenant,oid):
        return [dict(r) for (t,_),r in reversed(self.rows.items()) if t==tenant and r['observed_id']==oid][:50]
    async def prior_incident(self,tenant,oid,fingerprint):
        return next((r['incident_id'] for (t,_),r in reversed(self.rows.items()) if t==tenant and
            r['observed_id']==oid and r['fingerprint']==fingerprint and r['incident_id']), '')


def store_of(service):
    if getattr(service,'alert_receipts',None) is None:
        dsn=getattr(getattr(getattr(service,'history',None),'store',None),'dsn',None)
        service.alert_receipts=PgReceipts(dsn) if dsn else LocalReceipts()
    return service.alert_receipts


def receipt(decision,row,state):
    from ..sessions import ALERT_SESSION_PREFIX
    labels=row['labels'];oid=labels.get('observed_id','');sid=ALERT_SESSION_PREFIX+decision.fingerprint
    return dict(receipt_id='alr_'+uuid4().hex,observed_id=oid,fingerprint=decision.fingerprint,
        incident_id=decision.incident_id,session_id=sid,state=state,record=row['record'],
        received_at=row['received_at'],error='')
