"""Authenticated source-bound ingestion. Independent of chat write permissions."""
import hmac
import json
import os
from pathlib import Path
from datetime import datetime,timezone,timedelta
from fastapi import Request
from fastapi.responses import JSONResponse
from diagnostics.contract import validate_event,timestamp
from ..memory.gates import redact_text


def redact(value,depth=0):
    if depth>16:raise ValueError('payload nesting exceeds limit')
    if isinstance(value,dict):
        return {k:('***' if any(word in k.lower() for word in
            ('password','secret','token','dsn','authorization','credential')) else redact(v,depth+1))
            for k,v in value.items()}
    if isinstance(value,list):return [redact(v,depth+1) for v in value]
    if isinstance(value,str):return redact_text(value)[0]
    return value


class SourcePolicy:
    def __init__(self,bindings):
        self.bindings=bindings;self.counters={}

    def authenticate(self,auth):
        token=auth[7:] if auth.startswith('Bearer ') else ''
        match=next((b for b in self.bindings if os.getenv(b['token_env']) and
            hmac.compare_digest(token.encode(),os.getenv(b['token_env']).encode())),None)
        if match is None:raise PermissionError('source authentication failed')
        return match

    def authorize(self,auth,rows):
        match=self.authenticate(auth)
        if not rows or len(rows)>32:raise ValueError('batch size must be 1..32')
        from time import time
        key=match['token_env'];minute=int(time()//60)
        old,count=self.counters.get(key,(minute,0))
        count=count if old==minute else 0
        if count+len(rows)>match.get('events_per_minute',12000):raise ValueError('source rate limit')
        for row in rows:self.check(match,row)
        self.counters[key]=(minute,count+len(rows))
        return match

    def check(self,binding,row):
        validate_event(row);i=row['identity']
        expected=(binding['tenant'],binding['env'],binding.get('cluster'))
        if (i['tenant'],i['env'],i['cluster'])!=expected:raise PermissionError('source scope mismatch')
        if i['service'] not in binding['services'] or row['source'] not in binding['sources']:
            raise PermissionError('source target or type not permitted')
        if timestamp(row['occurred_at'])>datetime.now(timezone.utc)+timedelta(minutes=5):
            raise ValueError('event timestamp too far in future')


def from_env():
    path=os.getenv('AIOPS_DIAGNOSTIC_SOURCES','')
    if not path:return SourcePolicy([])
    p=Path(path)
    if p.stat().st_size>65536:raise ValueError('source bindings too large')
    bindings=json.loads(p.read_text(encoding='utf-8'))['sources']
    if len(bindings)>64:raise ValueError('source binding limit exceeded')
    return SourcePolicy(bindings)


def install(app,store=None,policy=None):
    if policy is None:
        try:policy=from_env()
        except (OSError,ValueError,KeyError):policy=SourcePolicy([])
    app.state.diagnostic_policy=policy;app.state.diagnostic_store=store
    @app.post('/v1/observations/batches')
    async def ingest(request:Request):
        if store is None:return JSONResponse({'error':'diagnostic_archive_unconfigured'},503)
        try:
            grant=getattr(request.state,'observed_grant',None)
            if grant is None:policy.authenticate(request.headers.get('authorization',''))
            body=await request.body()
            if len(body)>240000:return JSONResponse({'error':'batch_too_large'},413)
            data=json.loads(body)
            if set(data)!={'schema_version','events'} or type(data['schema_version']) is not int or data['schema_version'] not in (1,2):
                raise ValueError('invalid batch schema')
            rows=data['events']
            if grant is not None:
                from ..observed.ingestion import authorize
                await authorize(app.state.observed_catalog,grant,rows)
                if data['schema_version']!=2:raise ValueError('scoped batch requires version 2')
            else:
                policy.authorize(request.headers.get('authorization',''),rows)
                if data['schema_version']!=1 or any(r['schema_version']!=1 for r in rows):raise PermissionError('scoped events require registry credential')
            clean=[]
            for row in rows:
                row=dict(row,payload=redact(row['payload']),quality=redact(row['quality']),raw_ref=redact(row['raw_ref']))
                clean.append(validate_event(row))
            accepted=await store.save(clean)
            return JSONResponse({'accepted_ids':accepted},200)
        except PermissionError:return JSONResponse({'error':'source_not_authorized'},403)
        except (ValueError,KeyError,TypeError,RecursionError):
            return JSONResponse({'error':'invalid_diagnostic_batch'},422)
        except Exception:return JSONResponse({'error':'diagnostic_archive_unavailable'},503)
