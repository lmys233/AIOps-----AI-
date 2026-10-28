"""Count observed independent starts, never infer missing restarts."""
import os
from diagnostics.contract import event,timestamp
from .evidence import from_event


def evidence(rows,raw_ref,window):
    threshold=max(2,int(os.getenv('AIOPS_DIAGNOSTIC_RESTART_COUNT','3')))
    interval=max(1,float(os.getenv('AIOPS_DIAGNOSTIC_RESTART_WINDOW_SECONDS','300')))
    groups={};out=[]
    for row in rows:
        if row['kind']!='process_start' or row['source']!='process_observer' or row['quality']['status']!='available':continue
        i=row['identity'];groups.setdefault((i['service'],i['env'],i['cluster'],i['instance']),[]).append(row)
    for values in groups.values():
        values=sorted(values,key=lambda r:r['occurred_at'])
        for n,last in enumerate(values):
            near=[r for r in values[:n+1] if (timestamp(last['occurred_at'])-timestamp(r['occurred_at'])).total_seconds()<=interval]
            boots={r['identity']['boot_id'] for r in near}
            if len(boots)<threshold:continue
            observed=event(last['identity'],'process_restart_burst','process_observer',
                {'starts':len(boots),'interval_seconds':interval,'witness_ids':[r['event_id'] for r in near]},
                occurred_at=last['occurred_at'],coverage={'start':near[0]['occurred_at'],'end':last['occurred_at'],'complete':True})
            out.append(from_event(observed,'lifecycle_agent',raw_ref,window));break
    return out
