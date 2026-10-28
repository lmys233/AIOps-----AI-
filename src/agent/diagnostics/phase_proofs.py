"""Interval unions preserve overlap; nearby samples never establish causality."""
from diagnostics.contract import timestamp
from .proofs import observation
from .worker_proofs import holder_interval,contains


def union_seconds(rows,start,end):
    spans=sorted((max(start,timestamp(r['coverage']['start'])),min(end,timestamp(r['coverage']['end']))) for r in rows)
    merged=[]
    for left,right in spans:
        if right<=left:continue
        if merged and left<=merged[-1][1]:merged[-1]=(merged[-1][0],max(merged[-1][1],right))
        else:merged.append((left,right))
    return sum((right-left).total_seconds() for left,right in merged)


def request_phases(rows):
    results=[]
    for http in rows:
        request=http['correlation'].get('request_id');p=http['payload']
        if http['kind']!='http_request' or not request or p.get('total_ms',0)<1000:continue
        start,end=map(timestamp,(http['coverage']['start'],http['coverage']['end']))
        elapsed=(end-start).total_seconds()
        if elapsed<=0:continue
        related=[r for r in rows if r['identity']==http['identity'] and r['kind']=='request_phase' and
            r['correlation'].get('request_id')==request]
        for kinds,signal in [({'database_query','pool_acquire'},'same_request_database_dominant'),
                             ({'downstream_call'},'same_request_downstream_dominant')]:
            phases=[r for r in related if r['payload'].get('phase') in kinds]
            # 80% is an explicit diagnostic dominance criterion, not an SLO.
            if phases and union_seconds(phases,start,end)/elapsed>=.8:
                results.append(observation(signal,[http]+phases))
    return results


def pool_holders(rows):
    out=[]
    for timeout in rows:
        if timeout['kind']!='pool_timeout':continue
        at=timestamp(timeout['occurred_at']);pool=timeout['payload'].get('pool_id')
        related=[r for r in rows if r['identity']==timeout['identity']]
        held=holder_interval(related,pool,at)
        if not held:continue
        sessions={r['correlation'].get('session_id') for r in held if r['kind']=='pool_acquire'}
        sql=[r for r in related if r['kind'] in ('sql_query','sql_error') and
            r['correlation'].get('session_id') in sessions and contains(r,at) and r['payload'].get('purpose','business')=='business']
        executing={r['correlation'].get('session_id') for r in sql}
        if sessions and sessions==executing:
            proof=[timeout]+held+sql
            out.extend([observation('pool_full_during_timeout',proof),observation('holder_sql_spans_timeout',proof)])
    return out


def platform_exit(rows):
    out=[]
    for exit in rows:
        if exit['kind']!='process_exit' or exit['source']!='process_observer':continue
        for platform in rows:
            if platform['source']!='platform_event' or platform['identity']!=exit['identity']:continue
            if abs((timestamp(platform['occurred_at'])-timestamp(exit['occurred_at'])).total_seconds())>2:continue
            if platform['payload'].get('platform_oom_confirmed') is True:
                out.append(observation('platform_oom_matches_exit',[exit,platform]))
            if exit['payload'].get('signal') and platform['payload'].get('termination_signal')==exit['payload']['signal']:
                out.append(observation('external_signal_matches_exit',[exit,platform]))
    return out
