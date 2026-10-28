"""Semantic positive witnesses from structured blackboard observations."""
from diagnostics.contract import timestamp
from diagnostics.locks import wait_cycle
from ..attribution.v2.verification import Observation
from .evidence import events_of


def observation(signal,rows,mechanisms=()):
    first=rows[0];i=first['identity']
    from ..attribution.v2.facts import OBJECT,BARE_OBJECT
    text=' '.join(str(r['payload'].get('message','')) for r in rows)
    obj=OBJECT.search(text) or BARE_OBJECT.search(text)
    return Observation(signal=signal,service=i['service'],env=i['env'],cluster=i['cluster'] or '',
        request_id=first['correlation'].get('request_id',''),
        endpoint=first['payload'].get('route') or first['payload'].get('path') or '',
        instance=i['instance'],boot_id=i['boot_id'],query_fingerprint=first['payload'].get('query_fingerprint',''),
        object_name=obj[1] if obj else '',
        value=True,scope_verified=True,complete=True,
        time_range=(min(timestamp(r['coverage']['start']) for r in rows),
                    max(timestamp(r['coverage']['end']) for r in rows)),
        mechanism_keys=list(mechanisms),reason='代码核查结构化见证：'+','.join(r['event_id'] for r in rows))


def same_request_failure(rows):
    results=[]
    for http in rows:
        p=http['payload'];request=http['correlation'].get('request_id')
        if http['kind']!='http_request' or not request or not p.get('exception_type'):continue
        for sql in rows:
            q=sql['payload']
            if (sql['kind']=='sql_error' and sql['identity']==http['identity'] and
                sql['correlation'].get('request_id')==request and q.get('exception_type')==p.get('exception_type') and
                q.get('message')==p.get('message')):
                results.append(observation('same_request_database_failure',[http,sql],['http_database_failure']))
    return results


def deadlocks(rows):
    results=[]
    for error in rows:
        p=error['payload']
        if error['kind']!='sql_error' or p.get('sqlstate')!='40P01' or p.get('truncated'):continue
        cycle=wait_cycle(str(p.get('detail',''))+'\n'+str(p.get('message','')))
        if not cycle:continue
        actual=(p.get('database_identity') or {}).get('server_id')
        related=[r for r in rows if r['identity']==error['identity'] and
            r['kind'] in ('sql_query','sql_error') and r['payload'].get('server_pid') in cycle and
            (not actual or (r['payload'].get('database_identity') or {}).get('server_id')==actual)]
        start,end=map(timestamp,(error['coverage']['start'],error['coverage']['end']))
        # A PID can be reused by many transactions. Only transactions whose query
        # intervals overlap this deadlock can contribute their earlier lock order.
        active={r['correlation'].get('transaction_id') for r in related
                if timestamp(r['coverage']['start'])<=end and timestamp(r['coverage']['end'])>=start}
        related=[r for r in related if r['correlation'].get('transaction_id') in active and
                 timestamp(r['coverage']['start'])<=end]
        ordered={}
        for r in sorted(related,key=lambda r:r['occurred_at']):
            tx=r['correlation'].get('transaction_id');locks=r['payload'].get('lock_refs',[])
            if tx and locks:ordered.setdefault(tx,[]).extend(locks)
        sequences=list(ordered.values())
        conflicting=any(len(a)>=2 and len(b)>=2 and a[0]!=a[1] and a[:2]==list(reversed(b[:2]))
                        for a in sequences for b in sequences if a is not b)
        if conflicting:
            witnesses=[error]+[r for r in related if r['payload'].get('lock_refs')]
            results.extend([observation('transaction_wait_cycle',witnesses),
                            observation('conflicting_lock_order',witnesses)])
    return results


def sql_waits(rows):
    results=[]
    for sql in rows:
        p=sql['payload'];actual=p.get('database_identity') or {}
        if sql['kind']!='sql_query' or p.get('purpose','business')!='business':continue
        for db in rows:
            if db['kind']!='db_activity' or db['payload'].get('actual',{}).get('server_id')!=actual.get('server_id'):continue
            if not actual.get('server_id'):continue
            for activity in db['payload'].get('rows',[]):
                during=timestamp(sql['coverage']['start'])<=timestamp(db['occurred_at'])<=timestamp(sql['coverage']['end'])
                if (during and activity['pid']==p.get('server_pid') and activity.get('wait_event_type')=='Lock'
                    and activity.get('query_fingerprint')==p.get('query_fingerprint')):
                    results.append(observation('query_blocked_by_lock',[sql,db],['sql_lock_wait']))
    return results


def derive(evidence):
    return derive_rows(events_of(evidence))


def derive_rows(rows):
    rows=[r for r in rows if r['quality']['status']=='available' and not r['quality']['truncated']]
    from .worker_proofs import derive_workers
    from .phase_proofs import request_phases,pool_holders,platform_exit
    from .advanced_proofs import transaction_witnesses,comparable_plans
    from .human_proofs import verified_conditions
    from .cancellation_proofs import derive as derive_cancellations
    return (derive_cancellations(rows)+same_request_failure(rows)+deadlocks(rows)+sql_waits(rows)+derive_workers(rows)+
            request_phases(rows)+pool_holders(rows)+platform_exit(rows)+transaction_witnesses(rows)+comparable_plans(rows)+verified_conditions(rows))


async def publish(broker,state):
    from ..agents.base import AgentOutcome
    observations=derive(state.evidence);readable=set()
    for row in observations:
        runtime_signals={'same_request_database_failure','client_disconnect_cancelled_request',
            'configured_deadline_aborted_request','framework_shutdown_cancelled_request'}
        role='runtime_agent' if row.signal in runtime_signals else 'database_agent'
        item=broker.evidence(row,'diagnostic_derived:'+row.signal,role)
        await broker.board.publish(state.incident_id,role,[item])
        state.outcomes.append(AgentOutcome(role=role,evidence=[item]))
        if await broker.board.get_evidence(state.incident_id,item.evidence_id):readable.add(item.evidence_id)
    return observations,readable
