"""Only a signed platform request can claim active probe provenance."""
import hashlib, hmac, re


def correlation(runtime,scope):
    headers=dict(scope.get('headers',[]))
    run=headers.get(b'x-aiops-probe-id',b'').decode('ascii',errors='replace')
    rid=headers.get(b'x-aiops-request-id',b'').decode('ascii',errors='replace')
    proof=headers.get(b'x-aiops-probe-proof',b'').decode('ascii',errors='replace')
    worker=getattr(runtime,'registrar',None)
    token=worker.read_token() if worker else ''
    if not token or not re.fullmatch(r'probe_[a-f0-9]{32}',run) or not re.fullmatch(r'[a-f0-9]{32}',rid):return {}
    expected=hmac.new(token.encode(),(run+':'+rid).encode(),hashlib.sha256).hexdigest()
    return dict(request_id=rid,probe_id=run,traffic_origin='active_probe') if hmac.compare_digest(expected,proof) else {}
