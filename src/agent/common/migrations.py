"""Additive schema installations have transactional, cross-process receipts.

No down migrations or record retention operations run here. A changed DDL bundle
is applied under the component lock and receives a monotonically numbered receipt.
"""
from hashlib import sha256
import asyncio
from contextlib import asynccontextmanager


@asynccontextmanager
async def acquired(pool):
    # Complete/release a pending connection handshake before propagating shutdown.
    # Cancelling Windows asyncpg SSL negotiation can leave proactor callbacks on a
    # cancelled future. The pool's finite acquire deadline still bounds cleanup.
    task=asyncio.ensure_future(pool.acquire(timeout=5))
    try:
        connection=await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            connection=await task
            await pool.release(connection)
        except Exception:pass
        raise
    else:
        try:yield connection
        finally:await pool.release(connection)


async def install(pool, component, ddl):
    digest = sha256(ddl.encode()).hexdigest()
    async with acquired(pool) as connection, connection.transaction():
        # Serialize the receipt-table bootstrap too (concurrent CREATE IF NOT EXISTS
        # can still race on PostgreSQL catalog uniqueness).
        await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('aiops:schema',0))")
        await connection.execute('''CREATE TABLE IF NOT EXISTS agent_schema_versions(
            component TEXT NOT NULL, version INTEGER NOT NULL, digest TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(component,version),
            UNIQUE(component,digest))''')
        existing = await connection.fetchval('SELECT version FROM agent_schema_versions WHERE component=$1 AND digest=$2', component, digest)
        if existing is not None: return existing
        await connection.execute(ddl)
        version = await connection.fetchval('SELECT COALESCE(max(version),0)+1 FROM agent_schema_versions WHERE component=$1', component)
        await connection.execute('INSERT INTO agent_schema_versions(component,version,digest) VALUES($1,$2,$3)', component, version, digest)
        return version
