"""Only correlate waits that actually span the recorded rejection."""
from diagnostics.contract import timestamp
from .proofs import observation


def contains(row,at):
    return timestamp(row['coverage']['start'])<=at<=timestamp(row['coverage']['end'])


def holder_interval(rows,pool,at):
    holders={};capacity=None
    for acquire in rows:
        if acquire['kind']!='pool_acquire' or acquire['payload'].get('pool_id')!=pool:continue
        p=acquire['payload']
        if p.get('capacity',0)<=0:continue
        capacity=p['capacity'];began=timestamp(acquire['occurred_at'])
        releases=[r for r in rows if r['kind']=='pool_release' and r['identity']==acquire['identity'] and
            r['payload'].get('pool_id')==pool and r['correlation']==acquire['correlation'] and timestamp(r['occurred_at'])>=began]
        if not releases:continue
        release=min(releases,key=lambda r:r['occurred_at'])
        if began<=at<=timestamp(release['occurred_at']):
            holders[acquire['correlation'].get('session_id',acquire['event_id'])]=[acquire,release]
    return [r for pair in holders.values() for r in pair] if capacity and len(holders)>=capacity else []


def derive_workers(rows):
    out=[]
    for reject in rows:
        if reject['kind']!='worker_reject' or reject['payload'].get('reason')!='capacity':continue
        p=reject['payload'];at=timestamp(reject['occurred_at']);pool=p.get('pool_id')
        if p.get('active',0)<p.get('capacity',1):continue
        for phase in rows:
            if phase['kind']!='request_phase' or phase['identity']!=reject['identity']:continue
            if phase['correlation'].get('worker_pool_id')!=pool or not contains(phase,at):continue
            if phase['payload'].get('phase')=='pool_acquire':
                held=holder_interval([r for r in rows if r['identity']==reject['identity']],phase['payload'].get('resource_id'),at)
                if held:
                    proof=[reject,phase]+held
                    out.extend([observation('worker_database_wait',proof),observation('pool_pressure',proof)])
            if phase['payload'].get('phase')=='downstream_call':
                request=phase['correlation'].get('request_id')
                matching=[r for r in rows if r['kind']=='downstream_call' and r['identity']==phase['identity'] and
                    request and r['correlation'].get('request_id')==request and r['payload'].get('elapsed_ms',0)>500]
                if matching:
                    proof=[reject,phase,matching[0]]
                    out.extend([observation('worker_downstream_wait',proof),observation('correlated_downstream_wait',proof)])
    return out
