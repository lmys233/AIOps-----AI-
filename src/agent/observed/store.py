"""PG truth for registered systems; no in-memory registration or leases."""
from ..common.migrations import install
from ..common.lifecycle import close_pool
import asyncio,json
from .schema import DDL


def public(row):
    out=dict(row)
    for key,value in out.items():
        if hasattr(value,'isoformat'):out[key]=value.isoformat()
        if key in ('services','origins','envs','capabilities','details','source_types','endpoints') and isinstance(value,str):out[key]=json.loads(value)
    return out


class RegistryStore:
    ddl=DDL
    def __init__(self,dsn):self.dsn=dsn;self.pool=None;self.lock=asyncio.Lock()

    async def connect(self):
        import asyncpg
        async with self.lock:
            if self.pool is None:
                pool=await asyncpg.create_pool(self.dsn,min_size=0,max_size=4,timeout=3,command_timeout=5)
                try:
                    await install(pool,type(self).__module__+'.'+type(self).__name__,self.ddl)
                except BaseException:await close_pool(pool);raise
                self.pool=pool
        return self.pool

    async def fetch(self,sql,*args):
        pool=await self.connect()
        async with pool.acquire() as c:return [public(r) for r in await c.fetch(sql,*args)]

    async def one(self,sql,*args):
        rows=await self.fetch(sql,*args)
        return rows[0] if rows else None

    async def audit(self,c,tenant,oid,action,details):
        await c.execute('INSERT INTO agent_observed_audit(tenant,observed_id,action,details) VALUES($1,$2,$3,$4::jsonb)',
            tenant,oid,action,json.dumps(details))

    async def close(self):
        error=None
        for child in getattr(self,'children',[]):
            try:await child.close()
            except Exception as exc:error=error or exc
        pool,self.pool=self.pool,None
        try:await close_pool(pool)
        finally:
            if error:raise error
