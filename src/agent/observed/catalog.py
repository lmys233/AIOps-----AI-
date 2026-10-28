"""Safe registry directory; permission decisions always use PostgreSQL, not cached pages."""
import json
from .store import public


class Catalog:
    def __init__(self,store,redis=None):self.store=store;self.redis=redis

    async def page(self,tenant,before=None,limit=20):
        limit=max(1,min(int(limit),50));key='observed:directory:'+tenant+':'+str(before)+':'+str(limit)
        # Read the revision even on cache hits. Revocation cannot be hidden by TTL.
        revision=await self.store.one('SELECT coalesce(sum(revision),0)::text AS revision FROM agent_observed_systems WHERE tenant=$1',tenant)
        if self.redis:
            try:
                cached=await self.redis.get(key)
                if cached:
                    doc=json.loads(cached)
                    if doc['revision']==revision['revision']:return doc['page']
            except Exception:pass
        rows=await self.store.fetch('SELECT * FROM agent_observed_systems WHERE tenant=$1 AND ($2::text IS NULL OR observed_id>$2) ORDER BY observed_id LIMIT $3',tenant,before,limit+1)
        selected=rows[:limit]
        for row in selected:
            row['state']='registered_unverified' if row['registered_at'] else 'awaiting_registration'
            row['services']=await self.services(tenant,row['observed_id'])
            tables=await self.store.one("SELECT to_regclass('agent_tool_attempts') AS found")
            if tables and tables['found']:
                latest=await self.store.one('''SELECT body FROM agent_tool_attempts WHERE tenant=$1 AND observed_id=$2
                    AND transport='sdk_direct' AND (body->>'sent')::boolean ORDER BY finished_at DESC LIMIT 1''',tenant,row['observed_id'])
                if latest:row['last_access']=json.loads(latest['body']) if isinstance(latest['body'],str) else latest['body']
        page={'systems':selected,'has_more':len(rows)>limit,'next_cursor':selected[-1]['observed_id'] if len(rows)>limit else None}
        if self.redis:
            try:await self.redis.set(key,json.dumps({'revision':revision['revision'],'page':page}),ex=60)
            except Exception:pass
        return page

    async def services(self,tenant,oid):
        rows=await self.store.fetch('SELECT * FROM agent_observed_services WHERE tenant=$1 AND observed_id=$2 ORDER BY service',tenant,oid)
        for row in rows:
            row['instances']=await self.store.fetch('SELECT * FROM agent_observed_instances WHERE tenant=$1 AND observed_id=$2 AND service=$3 ORDER BY last_registered_at DESC',tenant,oid,row['service'])
            for i in row['instances']:i.pop('grant_id',None)
        return rows

    async def set_active(self,tenant,oid,active,service=None):
        pool=await self.store.connect()
        async with pool.acquire() as c:
            async with c.transaction():
                exists=await c.fetchval('SELECT 1 FROM agent_observed_systems WHERE tenant=$1 AND observed_id=$2 FOR UPDATE',tenant,oid)
                if not exists:raise KeyError('system not found')
                if service:
                    changed=await c.fetchval('UPDATE agent_observed_services SET active=$4 WHERE tenant=$1 AND observed_id=$2 AND service=$3 RETURNING 1',tenant,oid,service,active)
                    if not changed:raise KeyError('service not registered')
                else:await c.execute('UPDATE agent_observed_systems SET active=$3 WHERE tenant=$1 AND observed_id=$2',tenant,oid,active)
                await c.execute('UPDATE agent_observed_systems SET revision=revision+1 WHERE tenant=$1 AND observed_id=$2',tenant,oid)
                await self.store.audit(c,tenant,oid,'set_active',{'active':active,'service':service})

    async def rename(self,tenant,oid,name):
        from diagnostics.contract import identifier
        identifier(name);pool=await self.store.connect()
        async with pool.acquire() as c:
            async with c.transaction():
                changed=await c.fetchval('UPDATE agent_observed_systems SET name=$3,revision=revision+1 WHERE tenant=$1 AND observed_id=$2 RETURNING 1',tenant,oid,name)
                if not changed:raise KeyError('system not found')
                await self.store.audit(c,tenant,oid,'rename',{'name':name})

    async def revoke(self,gid):
        pool=await self.store.connect()
        async with pool.acquire() as c:
            async with c.transaction():
                g=await c.fetchrow('UPDATE agent_observed_grants SET active=false WHERE grant_id=$1 RETURNING tenant,observed_id',gid)
                if g:
                    await c.execute('UPDATE agent_observed_systems SET revision=revision+1 WHERE tenant=$1 AND observed_id=$2',g['tenant'],g['observed_id'])
                    await self.store.audit(c,g['tenant'],g['observed_id'],'revoke',{'grant_id':gid})
