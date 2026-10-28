"""Operator-configured database collector, independently scheduled from SDK."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.request import Request,urlopen
from .database import PostgresObserver,history_file,deadlock_log
from .lifecycle import Journal
from .outbox import Outbox


async def once(config,sink):
    notes=[]
    for mapping in config['sources']:
        observer=PostgresObserver(mapping,mapping['identity'])
        for kind in mapping.get('kinds',['db_activity','db_locks','db_context']):
            try:sink(await observer.read(kind))
            except Exception as exc:
                notes.append({'kind':kind,'error':type(exc).__name__})
                from .contract import event
                status='permission_denied' if getattr(exc,'sqlstate',None)=='42501' else 'unconfigured' if isinstance(exc,ValueError) else 'query_failed'
                sink(event(mapping['identity'],'capability_report','database_observer',
                    {'source_kind':kind,'status':status,'reason':type(exc).__name__},
                    quality={'status':status,'reason':type(exc).__name__,'sampled':False,'truncated':False,'dropped':0}))
        for key,kind,source in [('migration_file','db_migration','migration'),
                               ('ddl_audit_file','db_ddl','ddl_audit'),('plan_file','db_plan','query_plan'),
                               ('verification_file','verified_conditions','human_verification')]:
            if mapping.get(key):
                try:sink(history_file(mapping[key],mapping['identity'],kind,source))
                except Exception as exc:notes.append({'kind':kind,'error':type(exc).__name__})
        if mapping.get('deadlock_file'):
            try:
                path=Path(mapping['deadlock_file'])
                if path.stat().st_size>512000:raise ValueError('deadlock source exceeds limit')
                data=json.loads(path.read_text(encoding='utf-8'))
                for record in data['records'][:200]:sink(deadlock_log(record,mapping['identity']))
            except Exception as exc:notes.append({'kind':'db_deadlock','error':type(exc).__name__})
    return notes


def publish(rows,url,token):
    body=json.dumps({'schema_version':rows[0]['schema_version'],'events':rows}).encode()
    request=Request(url,body,{'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with urlopen(request,timeout=3) as response:
        result=json.loads(response.read(8192))
    if result.get('accepted_ids')!=[r['event_id'] for r in rows]:raise ValueError('batch not acknowledged')


async def run(args):
    config=json.loads(Path(args.config).read_text(encoding='utf-8'))
    systems={m['identity'].get('observed_id') for m in config['sources']}
    if len(systems)>1:raise ValueError('collector must have a single registered system scope')
    journal=Journal(args.journal)
    outbox=Outbox(str(args.journal)+'.outbox.sqlite3')
    while True:
        rows=[];notes=await once(config,rows.append)
        for row in rows:journal.write(row)
        outbox.put(rows)
        url=os.getenv('AIOPS_DIAGNOSTIC_COLLECTOR_URL','');token=os.getenv('AIOPS_DIAGNOSTIC_COLLECTOR_TOKEN','')
        if url and token:
            try:
                for _ in range(16):
                    batch=outbox.batch()
                    if not batch:break
                    await asyncio.to_thread(publish,batch,url,token)
                    outbox.ack([r['event_id'] for r in batch])
            except Exception as exc:notes.append({'kind':'delivery','error':type(exc).__name__})
        print(json.dumps({'collected':len(rows),'notes':notes,'delivery_dropped':outbox.dropped}))
        if args.once:
            outbox.close();return
        await asyncio.sleep(max(1,args.interval))


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--journal',required=True)
    p.add_argument('--once',action='store_true');p.add_argument('--interval',type=float,default=5)
    asyncio.run(run(p.parse_args()))


if __name__=='__main__':main()
