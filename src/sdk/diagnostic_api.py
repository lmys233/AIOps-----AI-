"""Optional authenticated endpoint; disabled when no diagnostic token is set."""
import hmac
import math
import os
from time import monotonic

PATHS=('/_aiops/capabilities','/_aiops/snapshot','/_aiops/probe-observations')


async def handle(runtime,scope,send):
    token=os.getenv('AIOPS_SDK_DIAGNOSTICS_TOKEN','')
    worker=getattr(runtime,'registrar',None)
    if worker and worker.read_token():token=worker.read_token()
    path=scope.get('path')
    if not token or path not in PATHS:return False
    from .app import _send_json
    headers=dict(scope.get('headers',[]))
    auth=headers.get(b'authorization',b'').decode('latin-1')
    if not hmac.compare_digest(auth.encode(),('Bearer '+token).encode()):
        await _send_json(send,401,{'error':'unauthorized'});return True
    if scope.get('method')!='GET':
        await _send_json(send,405,{'error':'read_only'});return True
    if path.endswith('probe-observations'):
        from urllib.parse import parse_qs
        import re
        query=parse_qs(scope.get('query_string',b'').decode('ascii',errors='replace'))
        run_id=query.get('run_id',[''])[0]
        if not re.fullmatch(r'probe_[a-f0-9]{32}',run_id):
            await _send_json(send,422,{'error':'invalid_probe_id'});return True
        with runtime.diagnostics.lock:
            events=[r for r in runtime.diagnostics.events if r['correlation'].get('probe_id')==run_id]
        from diagnostics.priority import priority
        import json
        selected=[];size=0
        for row in sorted(reversed(events),key=priority,reverse=True):
            count=len(json.dumps(row,ensure_ascii=False).encode())
            if len(selected)<100 and size+count<=500000:selected.append(row);size+=count
        await _send_json(send,200,{'identity':dict(runtime.diagnostics.identity),'events':selected,
            'truncated':len(events)>len(selected),'complete':False,'note':'本地有界诊断缓冲区，优先保留错误；不保证覆盖全部观测'})
        return True
    if path.endswith('snapshot'):
        last=getattr(runtime,'_last_diagnostic_snapshot',0)
        remaining=2-(monotonic()-last)
        if remaining>0:
            async def limited_send(message):
                if message['type']=='http.response.start':
                    message=dict(message,headers=message['headers']+[(b'retry-after',str(math.ceil(remaining)).encode('ascii'))])
                await send(message)
            await _send_json(limited_send,429,{'error':'snapshot_rate_limit','retry_after_sec':remaining});return True
        runtime._last_diagnostic_snapshot=monotonic()
        from .runtime_snapshot import snapshot
        row=snapshot(runtime,force_sample=True)
        await _send_json(send,200 if row else 503,row or {'error':'snapshot_unavailable'})
    else:await _send_json(send,200,runtime.diagnostics.snapshot())
    return True
