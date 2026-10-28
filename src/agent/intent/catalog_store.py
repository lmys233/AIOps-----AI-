"""PostgreSQL catalog truth, optimistic revisions, and reusable example vectors."""
from ..common.migrations import install
from ..common.lifecycle import close_pool
import asyncio
import json

from .catalog_models import defaults, digest

DDL = '''CREATE TABLE IF NOT EXISTS agent_intent_catalog (
 tenant TEXT PRIMARY KEY, revision TEXT NOT NULL, routes JSONB NOT NULL,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
 CREATE TABLE IF NOT EXISTS agent_intent_vectors (
 tenant TEXT NOT NULL, vector_key TEXT NOT NULL, provider TEXT NOT NULL,
 embedding JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant,vector_key));'''


class CatalogConflict(Exception):
    pass


class PgIntentStore:
    def __init__(self, dsn, tenant='default'):
        self.dsn, self.tenant = dsn, tenant
        self.pool = None
        self.lock = asyncio.Lock()

    async def connect(self):
        import asyncpg
        async with self.lock:
            if self.pool is None:
                pool = await asyncpg.create_pool(self.dsn, min_size=0, max_size=3,
                                                timeout=3, command_timeout=5)
                try:
                    await install(pool,'intent-catalog',DDL)
                    rows = defaults()
                    await pool.execute('''INSERT INTO agent_intent_catalog(tenant,revision,routes)
                        VALUES($1,$2,$3::jsonb) ON CONFLICT DO NOTHING''',
                        self.tenant, digest(rows), json.dumps(rows, ensure_ascii=False))
                except BaseException:
                    await close_pool(pool)
                    raise
                self.pool = pool
        return self.pool

    async def read(self):
        pool = await self.connect()
        row = await pool.fetchrow('SELECT * FROM agent_intent_catalog WHERE tenant=$1', self.tenant)
        return {'revision': row['revision'], 'routes': json.loads(row['routes'])}

    async def write(self, rows, expected):
        pool = await self.connect()
        # An opaque revision also distinguishes A -> B -> A from an old A writer.
        import uuid
        revision = digest([rows, uuid.uuid4().hex])
        row = await pool.fetchrow('''UPDATE agent_intent_catalog SET revision=$2,routes=$3::jsonb,
             updated_at=now() WHERE tenant=$1 AND revision=$4 RETURNING revision''',
             self.tenant, revision, json.dumps(rows, ensure_ascii=False), expected)
        if not row:
            raise CatalogConflict('意图目录已被修改，请刷新后重试')
        return {'revision': revision, 'routes': rows}

    async def vectors(self, keys):
        pool = await self.connect()
        rows = await pool.fetch('''SELECT vector_key,embedding FROM agent_intent_vectors
            WHERE tenant=$1 AND vector_key=ANY($2::text[])''', self.tenant, keys)
        return {r['vector_key']: json.loads(r['embedding']) for r in rows}

    async def save_vectors(self, provider, rows):
        pool = await self.connect()
        async with pool.acquire() as conn, conn.transaction():
            await conn.executemany('''INSERT INTO agent_intent_vectors VALUES($1,$2,$3,$4::jsonb,now())
                ON CONFLICT(tenant,vector_key) DO NOTHING''',
                [(self.tenant, k, provider, json.dumps(v)) for k, v in rows.items()])

    async def close(self):
        if self.pool:
            await close_pool(self.pool)
            self.pool = None
