"""Observe public asyncpg calls without replacing connections or transactions."""
from contextvars import ContextVar
import functools
import os
from time import perf_counter
from uuid import uuid4
from weakref import WeakKeyDictionary
from ..diagnostic_state import active_runtime,current_correlation
from ..diagnostic_http import exception_fields
from diagnostics.sql import sql_shape

BINDINGS=WeakKeyDictionary()
BUSY=ContextVar('aiops_query_recording',default=False)
METHODS=('execute','executemany','fetch','fetchrow','fetchval')


def binding(conn):
    if conn not in BINDINGS:
        runtime=active_runtime()
        if runtime is None or not runtime.diagnostics.enabled:return None
        BINDINGS[conn]=dict(runtime=runtime,session_id=uuid4().hex,transactions=[],identity=None)
    return BINDINGS[conn]


def safe_binding(conn):
    try:
        found=binding(conn)
        return found if found is not None and found['runtime'].diagnostics.enabled else None
    except Exception:return None


def query_wrapper(original):
    @functools.wraps(original)
    async def call(conn,query,*args,**kwargs):
        item=safe_binding(conn)
        if item is None or BUSY.get():return await original(conn,query,*args,**kwargs)
        token=BUSY.set(True);started=perf_counter();exc=None
        from diagnostics.contract import utc_now
        started_at=utc_now()
        if item.get('holder'):item['holder']['phase']='database_query'
        from ..diagnostic_stage import stage
        try:
            with stage(item['runtime'],'database_query'):
                return await original(conn,query,*args,**kwargs)
        except BaseException as error:
            exc=error;raise
        finally:
            BUSY.reset(token)
            if item.get('holder'):item['holder']['phase']='held'
            try:record_query(conn,item,original.__name__,query,started,exc,args,started_at)
            except Exception:item['runtime'].diagnostics.dropped+=1
    return call


def record_query(conn,item,method,query,started,exc,args,started_at):
    import re
    if not exc and item['identity'] and re.search(r'\b(?:SET|RESET|set_config)\b',str(query),re.I):
        # Preserve the actual server identity, but invalidate cached resolution
        # context after session changes. No extra business query is introduced.
        item['identity']=dict(item['identity'],schema=None,search_path=None,context_changed=True)
    payload=dict(sql_shape(query),method=method,execution_ms=(perf_counter()-started)*1000,
        outcome='error' if exc else 'success',database_identity=item['identity'],
        server_pid=conn.get_server_pid(),purpose=current_correlation().get('purpose','business'))
    if item.get('pool_id'):payload['pool_id']=item['pool_id']
    from diagnostics.locks import advisory_refs
    payload['lock_refs']=advisory_refs(query,args)
    if exc:
        payload.update(exception_fields(exc))
        match=re.search(r'''(?:relation|table|column)\s+["']([^"']+)["']''',payload['message'],re.I)
        if match:payload['object_name']=match[1][:128]
    corr=dict(current_correlation(),session_id=item['session_id'])
    if item['transactions']:corr['transaction_id']=item['transactions'][-1]
    from diagnostics.contract import utc_now
    now=utc_now()
    item['runtime'].diagnostics.emit('sql_error' if exc else 'sql_query',payload,'sdk_sql',corr,
        occurred_at=now,coverage=dict(start=started_at,end=now,complete=True))


async def connect_identity(conn,item):
    if os.getenv('AIOPS_SDK_DB_IDENTITY_READ','off').lower() not in ('on','true','1'):return
    if conn.is_in_transaction():return
    token=BUSY.set(True)
    try:
        row=await conn.fetchrow("SELECT current_database() AS database, current_user AS username, "
            "current_schema() AS schema, current_setting('search_path') AS search_path, "
            "COALESCE(inet_server_addr()::text,'local') AS server_address, inet_server_port() AS server_port",timeout=.5)
        from diagnostics.identity import database_identity
        item['identity']=database_identity(row)
        item['runtime'].diagnostics.capability('db_identity','available',adapter='opt-in-readonly-select')
        from diagnostics.contract import utc_now
        item['runtime'].diagnostics.capabilities['db_identity']['last_success']=utc_now()
    except Exception:
        item['runtime'].diagnostics.capability('db_identity','query_failed','Runtime identity read failed')
    finally:BUSY.reset(token)


def connect_wrapper(original):
    @functools.wraps(original)
    async def call(*args,**kwargs):
        conn=await original(*args,**kwargs)
        item=safe_binding(conn)
        if item is not None:
            try:await connect_identity(conn,item)
            except Exception:item['runtime'].diagnostics.dropped+=1
        return conn
    return call


def transaction_wrapper(original,phase):
    @functools.wraps(original)
    async def call(tx,*args,**kwargs):
        item=safe_binding(getattr(tx,'_connection',None));started=perf_counter();exc=None
        try:return await original(tx,*args,**kwargs)
        except BaseException as error:exc=error;raise
        finally:
            if item is not None:
                try:record_transaction(item,phase,args,started,exc)
                except Exception:item['runtime'].diagnostics.dropped+=1
    return call


def record_transaction(item,phase,args,started,exc):
    ids=item['transactions']
    if phase=='start' and not exc:ids.append(uuid4().hex)
    corr=dict(current_correlation(),session_id=item['session_id'])
    if ids:corr['transaction_id']=ids[-1]
    outcome=('rollback' if args and args[0] is not None else 'commit') if phase=='exit' else phase
    item['runtime'].diagnostics.emit('transaction_event',dict(phase=phase,outcome='error' if exc else outcome,
        duration_ms=(perf_counter()-started)*1000,nesting=len(ids)),'sdk_transaction',corr)
    if phase!='start' and ids:ids.pop()


def reset_wrapper(original):
    @functools.wraps(original)
    async def call(conn,*args,**kwargs):
        from ..diagnostic_state import CURRENT
        token=CURRENT.set(dict(CURRENT.get(),purpose='driver_reset'))
        try:return await original(conn,*args,**kwargs)
        finally:CURRENT.reset(token)
    return call


def patch(runtime):
    import asyncpg
    from asyncpg.transaction import Transaction
    for name in METHODS:
        original=getattr(asyncpg.Connection,name)
        if not getattr(original,'__aiops_query__',False):
            wrapped=query_wrapper(original);wrapped.__aiops_query__=True
            setattr(asyncpg.Connection,name,wrapped)
    for owner in (asyncpg,asyncpg.connection):
        if not getattr(owner.connect,'__aiops_query__',False):
            wrapped=connect_wrapper(owner.connect);wrapped.__aiops_query__=True;owner.connect=wrapped
    if not getattr(asyncpg.Connection.reset,'__aiops_query__',False):
        wrapped=reset_wrapper(asyncpg.Connection.reset);wrapped.__aiops_query__=True
        asyncpg.Connection.reset=wrapped
    for name,phase in [('start','start'),('commit','commit'),('rollback','rollback'),('__aexit__','exit')]:
        original=getattr(Transaction,name)
        if not getattr(original,'__aiops_query__',False):
            wrapped=transaction_wrapper(original,phase);wrapped.__aiops_query__=True
            setattr(Transaction,name,wrapped)
    runtime.diagnostics.capability('sdk_sql','available',adapter='asyncpg-public-methods')
    runtime.diagnostics.capability('sdk_transaction','available',adapter='asyncpg-transaction')
    runtime.diagnostics.capability('db_identity','partial','Opt-in identity SELECT; disabled by default')
