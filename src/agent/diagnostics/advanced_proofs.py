"""Historical formation witnesses require more than current empty snapshots."""
import json
from diagnostics.contract import timestamp
from diagnostics.locks import wait_cycle
from .proofs import observation


def transaction_witnesses(rows):
    out=[]
    for error in rows:
        if error['kind']!='sql_error' or error['payload'].get('sqlstate')!='40P01':continue
        cycle=wait_cycle(str(error['payload'].get('detail','')))
        if not cycle:continue
        related=[r for r in rows if r['identity']==error['identity'] and r['kind'] in ('sql_query','sql_error') and r['payload'].get('server_pid') in cycle]
        actual=(error['payload'].get('database_identity') or {}).get('server_id')
        for db in rows:
            if db['kind']!='db_activity' or not actual or db['payload'].get('actual',{}).get('server_id')!=actual:continue
            if not timestamp(error['coverage']['start'])<=timestamp(db['occurred_at'])<=timestamp(error['coverage']['end']):continue
            long=[a for a in db['payload'].get('rows',[]) if a['pid'] in cycle and a.get('transaction_age_seconds',0)>=30]
            for locks in rows:
                if locks['kind']!='db_locks' or locks['payload'].get('actual',{}).get('server_id')!=actual:continue
                if not timestamp(error['coverage']['start'])<=timestamp(locks['occurred_at'])<=timestamp(error['coverage']['end']):continue
                if any(l.get('granted') and l['pid'] in {a['pid'] for a in long} for l in locks['payload'].get('rows',[])):
                    witnesses=[error,db,locks]
                    out.extend([observation('long_transaction_holds_lock',witnesses),observation('transaction_wait_cycle',witnesses)])
        work={}
        for r in related:
            key=r['correlation'].get('work_id');tx=r['correlation'].get('transaction_id')
            if key and tx:work.setdefault(key,{}).setdefault(tx,[]).append(r)
        for transactions in work.values():
            values=list(transactions.values())
            if any({x for r in a for x in r['payload'].get('lock_refs',[])} & {x for r in b for x in r['payload'].get('lock_refs',[])}
                   for a in values for b in values if a is not b):
                witnesses=[error]+[r for v in values for r in v]
                out.extend([observation('duplicate_work_same_lock',witnesses),observation('transaction_wait_cycle',witnesses)])
    return out


def plan_nodes(tree,depth=0):
    if depth>12 or not isinstance(tree,dict):return set()
    out={tree.get('Node Type','')}
    for child in tree.get('Plans',[])[:32]:out.update(plan_nodes(child,depth+1))
    return out


def comparable_plans(rows):
    out=[]
    keys=('engine_version','schema_version','statistics_version','parameter_profile','resource_profile')
    for row in rows:
        if row['kind']!='db_plan' or row['source']!='query_plan' or row['payload'].get('verification_method')!='verified_human' or not row['coverage']['complete']:continue
        for record in row['payload'].get('records',[]):
            before,after=record.get('before',{}),record.get('after',{})
            if record.get('complete') is not True:continue
            try:
                if timestamp(before['captured_at'])>=timestamp(after['captured_at']):continue
            except (KeyError,ValueError,TypeError):continue
            if not record.get('query_fingerprint') or not all(before.get(k) and before.get(k)==after.get(k) for k in keys):continue
            if min(before.get('executions',0),after.get('executions',0))<5:continue
            old,new=before.get('plan'),after.get('plan')
            if not old or not new or json.dumps(old,sort_keys=True)==json.dumps(new,sort_keys=True):continue
            if not ('Index Scan' in plan_nodes(old) and 'Seq Scan' in plan_nodes(new)):continue
            if before.get('execution_p95_ms',0)<=0 or after.get('execution_p95_ms',0)<max(500,before['execution_p95_ms']*1.5):continue
            result=observation('comparable_plan_regression',[row],['plan_regression'])
            result.query_fingerprint=record['query_fingerprint']
            result.reason+='；已核实来源中的可比条件、前后计划与运行统计；不执行 EXPLAIN ANALYZE'
            out.append(result)
    return out
