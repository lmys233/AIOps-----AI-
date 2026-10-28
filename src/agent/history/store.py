"""平台 PostgreSQL 档案：会话内序号、唯一轮次、游标查询。"""
from ..common.migrations import install
from ..common.lifecycle import close_pool
import asyncio
import json
from datetime import datetime

SCHEMA='''CREATE TABLE IF NOT EXISTS agent_chat_turns (
 tenant_id TEXT NOT NULL, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
 turn_no BIGINT NOT NULL, incident_id TEXT NOT NULL DEFAULT '',
 created_at TIMESTAMPTZ NOT NULL, payload JSONB NOT NULL,
 PRIMARY KEY (tenant_id,session_id,turn_id), UNIQUE (tenant_id,session_id,turn_no));
 CREATE INDEX IF NOT EXISTS agent_chat_incident_idx ON agent_chat_turns(tenant_id,incident_id);
 CREATE TABLE IF NOT EXISTS agent_chat_deleted_sessions(tenant_id TEXT,session_id TEXT,PRIMARY KEY(tenant_id,session_id));
 CREATE TABLE IF NOT EXISTS agent_chat_incidents(tenant_id TEXT,incident_id TEXT,body JSONB NOT NULL,PRIMARY KEY(tenant_id,incident_id));'''


class HistoryDeleted(Exception):pass


class PgHistoryStore:
    def __init__(self,dsn,tenant='default'):
        self.dsn,self.tenant=dsn,tenant
        self.pool=None;self.lock=asyncio.Lock()

    async def connection(self):
        async with self.lock:
            if self.pool is None:
                import asyncpg
                pool=await asyncpg.create_pool(self.dsn,min_size=0,max_size=4,timeout=4,command_timeout=5)
                try:await install(pool,'chat-history',SCHEMA)
                except BaseException:
                    await close_pool(pool);raise
                self.pool=pool
        return self.pool

    async def save(self,sid,tid,payload):
        pool=await self.connection()
        async with pool.acquire() as conn,conn.transaction():
            return await self.save_using(conn,sid,tid,payload)

    async def save_using(self,conn,sid,tid,payload):
        from ..observed.context import current
        scope=current()
        raw=json.dumps(payload,ensure_ascii=False)
        if len(raw.encode())>3_000_000:raise ValueError('完整聊天档案超过 3MB 上限')
        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',self.tenant+':'+sid)
        if scope and await conn.fetchval("SELECT to_regclass('agent_system_session_views')"):
            if await conn.fetchval('SELECT deleted FROM agent_system_session_views WHERE tenant=$1 AND observed_id=$2 AND session_id=$3',self.tenant,scope.observed_id,sid):
                raise HistoryDeleted('system session deleted')
        if await conn.fetchval('SELECT 1 FROM agent_chat_deleted_sessions WHERE tenant_id=$1 AND session_id=$2',self.tenant,sid):
            raise HistoryDeleted('会话已删除')
        number=await conn.fetchval('SELECT COALESCE(MAX(turn_no),0)+1 FROM agent_chat_turns WHERE tenant_id=$1 AND session_id=$2',self.tenant,sid)
        at=datetime.fromisoformat(payload['at'])
        number=await conn.fetchval('''INSERT INTO agent_chat_turns
            (tenant_id,session_id,turn_id,turn_no,incident_id,created_at,payload)
            VALUES($1,$2,$3,$4,$5,$6,$7::jsonb) ON CONFLICT(tenant_id,session_id,turn_id)
            DO UPDATE SET payload=EXCLUDED.payload RETURNING turn_no''',
            self.tenant,sid,tid,number,payload.get('incident_id',''),at,raw)
        body=payload.get('response') or {}
        if body.get('incident_id'):
            await conn.execute('''INSERT INTO agent_chat_incidents VALUES($1,$2,$3::jsonb)
                ON CONFLICT(tenant_id,incident_id) DO UPDATE SET body=EXCLUDED.body''',
                self.tenant,body['incident_id'],json.dumps(body,ensure_ascii=False))
        return number

    async def readable(self,sid,observed_id=None):
        pool=await self.connection()
        if await pool.fetchval('SELECT 1 FROM agent_chat_deleted_sessions WHERE tenant_id=$1 AND session_id=$2',self.tenant,sid):return False
        if observed_id and await pool.fetchval("SELECT to_regclass('agent_system_session_views')"):
            if await pool.fetchval('SELECT deleted FROM agent_system_session_views WHERE tenant=$1 AND observed_id=$2 AND session_id=$3',self.tenant,observed_id,sid):return False
        return True

    async def page(self,sid,before=None,observed_id=None):
        if not await self.readable(sid,observed_id):return {'turns':[],'has_more':False,'next_cursor':None,'page_size':20}
        pool=await self.connection()
        data=await pool.fetch('''SELECT turn_no,turn_id,payload FROM agent_chat_turns
            WHERE tenant_id=$1 AND session_id=$2 AND ($3::bigint IS NULL OR turn_no<$3)
            AND ($4::text IS NULL OR payload#>>'{response,explanation,observed_system,observed_id}'=$4)
            ORDER BY turn_no DESC LIMIT 21''',self.tenant,sid,before,observed_id)
        more=len(data)>20;data=data[:20]
        rows=[dict(json.loads(r['payload']),turn=int(r['turn_no']),turn_id=r['turn_id']) for r in reversed(data)]
        return {'turns':rows,'has_more':more,'next_cursor':str(rows[0]['turn']) if rows and more else None,'page_size':20}

    async def delete(self,sid):
        pool=await self.connection()
        async with pool.acquire() as conn,conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',self.tenant+':'+sid)
            await conn.execute('INSERT INTO agent_chat_deleted_sessions VALUES($1,$2) ON CONFLICT DO NOTHING',self.tenant,sid)
            # Logical deletion fences reads/writes. Physical retention is a separate reviewed policy.

    async def incident(self,incident_id):
        pool=await self.connection()
        raw=await pool.fetchval('SELECT body FROM agent_chat_incidents WHERE tenant_id=$1 AND incident_id=$2',self.tenant,incident_id)
        return json.loads(raw) if raw else None

    async def task_turns(self,sid,target=''):
        from ..observed.context import current
        scope=current();oid=scope.observed_id if scope else None
        if not await self.readable(sid,oid):return []
        pool=await self.connection()
        rows=await pool.fetch('''SELECT payload FROM (
            SELECT DISTINCT ON (COALESCE(payload#>>'{response,explanation,dialogue,task_id}',incident_id))
                payload,turn_no FROM agent_chat_turns WHERE tenant_id=$1 AND session_id=$2
                AND payload#>'{response,explanation,attribution_v2,problems}' IS NOT NULL
                AND ($4::text IS NULL OR payload#>>'{response,explanation,observed_system,observed_id}'=$4)
                AND ($3='' OR incident_id=$3 OR payload#>>'{response,explanation,dialogue,task_id}'=$3)
            ORDER BY COALESCE(payload#>>'{response,explanation,dialogue,task_id}',incident_id),turn_no DESC
            ) versions ORDER BY turn_no DESC LIMIT 8''',self.tenant,sid,target,oid)
        return [json.loads(r['payload'])['response'] for r in rows]

    async def close(self):
        pool,self.pool=self.pool,None
        await close_pool(pool)

    async def dialogue_context(self,sid):
        from ..observed.context import current
        scope=current();oid=scope.observed_id if scope else None
        """Bounded legacy clarification recovery, including turns without a snapshot."""
        if not await self.readable(sid,scope.observed_id if scope else None):return []
        pool=await self.connection()
        rows=await pool.fetch('''SELECT payload FROM agent_chat_turns
            WHERE tenant_id=$1 AND session_id=$2 AND ($3::text IS NULL OR payload#>>'{response,explanation,observed_system,observed_id}'=$3)
            ORDER BY turn_no DESC LIMIT 3''',self.tenant,sid,oid)
        return [json.loads(r['payload']) for r in rows]

    async def model_turns(self,sid,oid,limit=20):
        if not await self.readable(sid,oid):return []
        pool=await self.connection()
        rows=await pool.fetch('''SELECT payload,turn_no FROM agent_chat_turns
            WHERE tenant_id=$1 AND session_id=$2 AND payload#>>'{response,explanation,observed_system,observed_id}'=$3
            ORDER BY turn_no DESC LIMIT $4''',self.tenant,sid,oid,min(20,max(1,limit)))
        return [dict(json.loads(r['payload']),turn=r['turn_no']) for r in reversed(rows)]

    async def context_turns(self,sid,oid,task_id='',anchor=None,env=None,cluster=None):
        if not await self.readable(sid,oid):return []
        """Only prompt fields, scoped and frozen before the execution's query anchor."""
        pool=await self.connection()
        rows=await pool.fetch('''SELECT turn_id,turn_no,payload->>'user_message' AS message,
            payload->>'at' AS at,incident_id,payload#>>'{answer,summary}' AS summary,
            payload#>>'{response,slots,time_window}' AS time_window,
            payload#>'{response,explanation,attribution_v2,scope,time_range}' AS time_range,
            payload#>>'{response,explanation,task_control,task_id}' AS task_id,
            payload#>>'{response,explanation,task_control,revision}' AS revision
            FROM agent_chat_turns WHERE tenant_id=$1 AND session_id=$2
            AND payload#>>'{response,explanation,observed_system,observed_id}'=$3
            AND ($4='' OR payload#>>'{response,explanation,task_control,task_id}'=$4)
            AND ($5::text IS NULL OR created_at<=($5::text)::timestamptz)
            AND ($6::text IS NULL OR payload#>>'{response,slots,env}'=$6)
            AND COALESCE(payload#>>'{response,slots,cluster,name}',payload#>>'{response,slots,cluster}','')=COALESCE($7,'')
            AND NOT EXISTS(SELECT 1 FROM agent_chat_deleted_sessions d WHERE d.tenant_id=$1 AND d.session_id=$2)
            ORDER BY turn_no DESC LIMIT 20''',self.tenant,sid,oid,task_id,anchor,env,cluster)
        from ..llm.context import digest
        out=[]
        for row in reversed(rows):
            item={'turn_id':row['turn_id'],'turn':row['turn_no'],'at':row['at'],'incident_id':row['incident_id'],
                  'user_message':row['message'] or '', 'summary':row['summary'] or '',
                  'task_id':row['task_id'] or '', 'revision':row['revision'] or '',
                  'window':{'requested':row['time_window'],'time_range':json.loads(row['time_range']) if row['time_range'] else None},
                  'url':f"/v1/observed-systems/{oid}/sessions/{sid}?before={row['turn_no']+1}"}
            item['content_hash']=digest(item);out.append(item)
        return out
