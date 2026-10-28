"""Explain sampling and later success without claiming global recovery."""
from diagnostics.contract import timestamp
from .evidence import events_of


def annotate(snapshot,evidence):
    rows=events_of(evidence)
    for p in snapshot.problems:
        from .evidence import fault
        failures=[r for r in rows if r['identity']['service']==p.service and fault(r) and fault(r)[0]==p.error_type and
            (not p.request_id or r['correlation'].get('request_id')==p.request_id) and
            (not p.instance or r['identity']['instance']==p.instance) and (not p.boot_id or r['identity']['boot_id']==p.boot_id)]
        later=[]
        for error in failures:
            fp=error['payload'].get('query_fingerprint');route=error['payload'].get('route')
            for success in rows:
                if success['identity']!=error['identity'] or timestamp(success['occurred_at'])<=timestamp(error['occurred_at']):continue
                same_query=bool(fp and success['kind']=='sql_query' and success['payload'].get('query_fingerprint')==fp)
                same_route=bool(route and success['kind']=='http_request' and success['payload'].get('route')==route and
                    success['payload'].get('outcome')=='complete' and (success['payload'].get('status') or 0)<400)
                if same_query or same_route:later.append(success)
        if later:
            p.current_state='later_success_observed'
            p.current_state_note='之后有同类成功样本；不能据此证明全量恢复或此刻正常'
    return snapshot
