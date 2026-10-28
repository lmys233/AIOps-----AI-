"""Per-request causal witnesses, never infer a source from exception names."""
from diagnostics.contract import timestamp
from .proofs import observation


def same_request(a,b):
    request=a['correlation'].get('request_id')
    return bool(request and request==b['correlation'].get('request_id') and a['identity']==b['identity'])


def within(failure,witness):
    start=failure['payload'].get('request_started_at')
    if not start:return False
    return timestamp(start)<=timestamp(witness['occurred_at'])<=timestamp(failure['occurred_at'])


def proof(signal,http,rows):
    record=observation(signal,[http]+rows)
    record.request_id=http['correlation']['request_id']
    p=http['payload'];route=p.get('route','')
    record.endpoint=f"{p.get('method','')} {route}" if route.startswith('/') else ''
    record.time_range=(timestamp(p['request_started_at']),timestamp(http['occurred_at']))
    return record


def derive(rows):
    rows=[r for r in rows if r['coverage']['complete'] and not r['quality']['dropped']]
    out=[];index={r['event_id']:r for r in rows}
    for http in rows:
        p=http['payload']
        if http['kind']!='http_request' or p.get('completed') or p.get('outcome') not in ('cancelled','timeout'):continue
        if not http['correlation'].get('request_id') or not p.get('request_started_at'):continue
        deadline=index.get(p.get('deadline_trigger_event_id'))
        if deadline and same_request(http,deadline) and within(http,deadline):
            d=deadline['payload']
            if deadline['kind']=='request_deadline' and d.get('expired') is True and d.get('translated_to_timeout') is True:
                out.append(proof('configured_deadline_aborted_request',http,[deadline]))
        cancel=index.get(p.get('cancel_trigger_event_id'))
        if cancel and same_request(http,cancel) and within(http,cancel) and cancel['kind']=='request_cancel':
            c=cancel['payload'];disconnect=index.get(c.get('trigger_event_id'))
            if c.get('accepted') is True and c.get('origin')=='client_disconnect' and disconnect and p.get('cancel_count')==1:
                d=disconnect['payload']
                if (disconnect['kind']=='request_disconnect' and same_request(http,disconnect) and within(http,disconnect)
                    and timestamp(disconnect['occurred_at'])<=timestamp(cancel['occurred_at']) and not d.get('received_after_response')):
                    out.append(proof('client_disconnect_cancelled_request',http,[disconnect,cancel]))
        shutdown=index.get(p.get('shutdown_trigger_event_id'))
        if shutdown and same_request(http,shutdown) and within(http,shutdown):
            s=shutdown['payload']
            if (shutdown['kind']=='request_shutdown_cancel' and s.get('adapter')=='uvicorn_cancel_branch'
                and s.get('cancel_count_before')==0 and p.get('cancel_count')==1):
                out.append(proof('framework_shutdown_cancelled_request',http,[shutdown]))
    return out
