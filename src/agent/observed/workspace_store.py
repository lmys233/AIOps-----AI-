"""System-bound new sessions; historical mixed sessions have separately scoped views."""
from .store import RegistryStore
from ..history.store import SCHEMA

DDL=SCHEMA+'''CREATE TABLE IF NOT EXISTS agent_system_sessions(
 tenant text NOT NULL,session_id text NOT NULL,observed_id text NOT NULL,title text NOT NULL DEFAULT '',
 created_at timestamptz NOT NULL DEFAULT now(),selected_at timestamptz,
 PRIMARY KEY(tenant,session_id));
 CREATE TABLE IF NOT EXISTS agent_system_session_views(
 tenant text,session_id text,observed_id text,title text NOT NULL DEFAULT '',
 selected_at timestamptz,deleted boolean NOT NULL DEFAULT false,PRIMARY KEY(tenant,session_id,observed_id));
 CREATE INDEX IF NOT EXISTS chat_system_scope ON agent_chat_turns
 (tenant_id,(payload#>>'{response,explanation,observed_system,observed_id}'),session_id,turn_no DESC);'''
PATH="payload#>>'{response,explanation,observed_system,observed_id}'"


class WorkspaceStore(RegistryStore):
    ddl=DDL
    async def bind(self,tenant,oid,sid,title=''):
        await self.fetch('INSERT INTO agent_system_sessions(tenant,observed_id,session_id,title) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING RETURNING session_id',tenant,oid,sid,title)
        row=await self.one('SELECT observed_id FROM agent_system_sessions WHERE tenant=$1 AND session_id=$2',tenant,sid)
        if row['observed_id']!=oid:raise PermissionError('session bound to another system')

    async def owned(self,tenant,oid,sid):
        if await self.one('SELECT 1 FROM agent_chat_deleted_sessions WHERE tenant_id=$1 AND session_id=$2',tenant,sid):raise KeyError('session deleted')
        bound=await self.one('SELECT observed_id FROM agent_system_sessions WHERE tenant=$1 AND session_id=$2',tenant,sid)
        if bound and bound['observed_id']!=oid:raise PermissionError('session belongs to another system')
        view=await self.one('SELECT deleted FROM agent_system_session_views WHERE tenant=$1 AND observed_id=$2 AND session_id=$3',tenant,oid,sid)
        if view and view['deleted']:raise KeyError('system session deleted')
        if not bound and not await self.one('SELECT 1 FROM agent_chat_turns WHERE tenant_id=$1 AND session_id=$2 AND '+PATH+'=$3 LIMIT 1',tenant,sid,oid):
            raise KeyError('no session in system')

    async def listing(self,tenant,oid,limit=50,sid=None):
        rows=await self.fetch('''SELECT ids.session_id,b.title AS bound_title,v.title AS view_title,
            coalesce(v.selected_at,b.selected_at) AS selected_at,b.created_at,t.message,t.incident_id,t.created_at AS last_at,t.total
            FROM (SELECT session_id FROM agent_system_sessions WHERE tenant=$1 AND observed_id=$2
                UNION SELECT session_id FROM agent_chat_turns WHERE tenant_id=$1 AND '''+PATH+'''=$2) ids
            LEFT JOIN agent_system_sessions b ON b.tenant=$1 AND b.session_id=ids.session_id AND b.observed_id=$2
            LEFT JOIN agent_system_session_views v ON v.tenant=$1 AND v.session_id=ids.session_id AND v.observed_id=$2
            LEFT JOIN LATERAL (SELECT payload->>'user_message' AS message,payload->>'incident_id' AS incident_id,
                created_at,count(*) OVER() AS total FROM agent_chat_turns
                WHERE tenant_id=$1 AND session_id=ids.session_id AND '''+PATH+'''=$2 ORDER BY turn_no DESC LIMIT 1) t ON true
            WHERE coalesce(v.deleted,false)=false AND ($4::text IS NOT NULL OR ids.session_id NOT LIKE 'alert_%')
                AND NOT EXISTS(SELECT 1 FROM agent_chat_deleted_sessions d WHERE d.tenant_id=$1 AND d.session_id=ids.session_id)
                AND ($4::text IS NULL OR ids.session_id=$4)
            ORDER BY coalesce(t.created_at,b.created_at) DESC LIMIT $3''',tenant,oid,max(1,min(limit,100)),sid)
        return [session_view(r,oid) for r in rows]

    async def update_view(self,tenant,oid,sid,title=None,select=False):
        await self.owned(tenant,oid,sid)
        await self.fetch('''INSERT INTO agent_system_session_views(tenant,observed_id,session_id,title,selected_at)
            VALUES($1,$2,$3,coalesce($4,''),CASE WHEN $5 THEN now() ELSE NULL END)
            ON CONFLICT(tenant,session_id,observed_id) DO UPDATE SET title=coalesce($4,agent_system_session_views.title),
            selected_at=CASE WHEN $5 THEN now() ELSE agent_system_session_views.selected_at END RETURNING 1''',tenant,oid,sid,title,select)

    async def remove(self,tenant,oid,sid):
        await self.owned(tenant,oid,sid);pool=await self.connect()
        async with pool.acquire() as c,c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',tenant+':'+sid)
            await c.execute('''INSERT INTO agent_system_session_views(tenant,observed_id,session_id,deleted) VALUES($1,$2,$3,true)
                ON CONFLICT(tenant,session_id,observed_id) DO UPDATE SET deleted=true''',tenant,oid,sid)
            # Keep the immutable archive; this system's tombstone controls visibility.


def session_view(row,oid):
    message=row.get('message') or '';sid=row['session_id']
    return {'session_id':sid,'observed_id':oid,'title':row.get('view_title') or row.get('bound_title') or message[:60] or '新会话',
        'last_message':message,'turn_count':row.get('total') or 0,'created_at':row.get('created_at') or row.get('last_at'),
        'last_turn_at':row.get('last_at'),'incident_ids':[row['incident_id']] if row.get('incident_id') else [],
        'is_last':bool(row.get('selected_at')),'selected_at':row.get('selected_at')}
