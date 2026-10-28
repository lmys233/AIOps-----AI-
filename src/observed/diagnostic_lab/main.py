"""Operator-triggered fault scenarios in a dedicated PostgreSQL schema."""
import asyncio
from contextlib import asynccontextmanager
import os
from uuid import uuid4
import asyncpg
from fastapi import FastAPI

POOL=None
SCHEMA='diagnostic_lab_'+uuid4().hex
PREFIX=os.getenv('DIAGNOSTIC_LAB_PREFIX','/fault')


@asynccontextmanager
async def lifespan(app):
    global POOL
    dsn=os.getenv('DIAGNOSTIC_LAB_DSN')
    if not dsn:raise RuntimeError('DIAGNOSTIC_LAB_DSN required; no business DSN fallback')
    admin=await asyncpg.connect(dsn)
    await admin.execute('CREATE SCHEMA "'+SCHEMA+'"')
    POOL=await asyncpg.create_pool(dsn,min_size=1,max_size=2,server_settings={'search_path':SCHEMA})
    try:yield
    finally:
        await POOL.close()
        await admin.execute('DROP SCHEMA "'+SCHEMA+'" CASCADE')
        await admin.close()


app=FastAPI(lifespan=lifespan)


@app.get(PREFIX+'/error')
async def error(recovered:bool=False):
    if recovered:return {'scenario':'application_recovered'}
    raise RuntimeError('isolated application exception')


@app.get(PREFIX+'/slow')
async def slow(recovered:bool=False):await asyncio.sleep(.001 if recovered else 1.1);return {'scenario':'slow_request'}


@app.get(PREFIX+'/missing')
async def missing():
    import re
    name=os.getenv('DIAGNOSTIC_LAB_OBJECT_NAME','journal_rows')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}',name):raise ValueError('invalid laboratory object name')
    return await POOL.fetch('SELECT * FROM "'+name+'"')


@app.get(PREFIX+'/slow-sql')
async def slow_sql(recovered:bool=False):
    await POOL.execute('SELECT pg_sleep($1)',.001 if recovered else .6);return {'scenario':'slow_sql'}


@app.get(PREFIX+'/pool')
async def pool_timeout(recovered:bool=False):
    if recovered:return {'value':await POOL.fetchval('SELECT 1')}
    a=await POOL.acquire();b=await POOL.acquire()
    try:await POOL.acquire(timeout=.05)
    finally:await POOL.release(a);await POOL.release(b)


@app.get(PREFIX+'/deadlock')
async def deadlock(recovered:bool=False):
    if recovered:return {'scenario':'sequential_transactions','value':await POOL.fetchval('SELECT 1')}
    key=int(uuid4().hex[:7],16)
    a=await POOL.acquire();b=await POOL.acquire();ta=a.transaction();tb=b.transaction()
    await ta.start();await tb.start()
    await a.execute('SELECT pg_advisory_xact_lock($1)',key)
    await b.execute('SELECT pg_advisory_xact_lock($1)',key+1)
    async def opposite(c,value):
        try:await c.execute('SELECT pg_advisory_xact_lock($1)',value,timeout=3);return 'completed'
        except asyncpg.DeadlockDetectedError:return 'deadlock_reported'
    try:return {'results':await asyncio.gather(opposite(a,key+1),opposite(b,key))}
    finally:
        await ta.rollback();await tb.rollback();await POOL.release(a);await POOL.release(b)


@app.get(PREFIX+'/worker')
async def worker(recovered:bool=False):
    if recovered:return {'scenario':'worker_available'}
    from sdk.diagnostic_state import active_runtime,CURRENT
    from sdk.workers import register_worker
    r=active_runtime();monitor=register_worker(r,1,0)
    entered=asyncio.Event();release=asyncio.Event()
    async def execute(fn):
        key=monitor.submit()
        if monitor.active>=monitor.capacity:
            monitor.reject(key,'capacity');raise RuntimeError('bounded worker queue rejected task')
        monitor.start(key);token=CURRENT.set(dict(CURRENT.get(),task_id=key,worker_pool_id=monitor.pool_id))
        try:return await fn()
        finally:CURRENT.reset(token);monitor.finish(key)
    async def hold():entered.set();await release.wait()
    busy=asyncio.create_task(execute(hold))
    try:
        await entered.wait()
        try:await execute(lambda:asyncio.sleep(.01))
        except RuntimeError:return {'scenario':'bounded_worker_rejection','rejected':True}
    finally:
        release.set();await busy;r.diagnostics.workers.pop(monitor.pool_id,None)
