"""Optional background exporter. No network or disk I/O in SDK emit()."""
import json
import os
from pathlib import Path
from threading import Event,Thread
from urllib.request import Request,urlopen
from urllib.parse import urlsplit
from .outbox import Outbox


class Exporter:
    def __init__(self,runtime,url,token,path,transport=None):
        parsed=urlsplit(url)
        if parsed.scheme not in ('http','https') or parsed.username or parsed.password:
            raise ValueError('invalid diagnostic endpoint')
        self.runtime=runtime;self.url=url;self.token=token;self.path=path
        self.transport=transport or self.send;self.stop_event=Event();self.thread=None
        self.seen=[];self.failures=0;self.delivered=0;self.outbox=None
        self.last_report=0
        self.last_snapshot=0
        try:runtime.diagnostic_snapshot_interval=max(2,float(os.getenv('AIOPS_SDK_SNAPSHOT_INTERVAL','0'))) if float(os.getenv('AIOPS_SDK_SNAPSHOT_INTERVAL','0'))>0 else 0
        except ValueError:runtime.diagnostic_snapshot_interval=0

    def send(self,rows):
        body=json.dumps({'schema_version':rows[0]['schema_version'],'events':rows},ensure_ascii=False).encode()
        request=Request(self.url,body,{'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        with urlopen(request,timeout=2) as response:
            result=json.loads(response.read(8192))
        if result.get('accepted_ids')!=[r['event_id'] for r in rows]:
            raise ValueError('diagnostic receiver did not acknowledge exact batch')

    def flush(self):
        from .system_scope import export_ready
        if not export_ready(self.runtime):return
        if self.outbox is None:self.outbox=Outbox(self.path)
        d=self.runtime.diagnostics
        from time import monotonic
        if monotonic()-self.last_report>30:
            d.emit('capability_report',dict(d.snapshot(),export_failures=self.failures,
                outbox_dropped=self.outbox.dropped),'sdk_runtime')
            self.last_report=monotonic()
            d.emit('sdk_heartbeat',{'pid':os.getpid(),'sdk_schema_version':1},'sdk_lifecycle')
        interval=self.runtime.diagnostic_snapshot_interval
        if interval and monotonic()-self.last_snapshot>=interval:
            from .runtime_snapshot import snapshot
            snapshot(self.runtime)
            self.last_snapshot=monotonic()
        seen=set(self.seen)
        with d.lock:rows=[r for r in d.events if r['event_id'] not in seen]
        if rows:
            self.outbox.put(rows)
            self.seen=(self.seen+[r['event_id'] for r in rows])[-2048:]
        batch=self.outbox.batch()
        if not batch:return
        try:self.transport(batch)
        except Exception:
            self.failures+=1;d.capability('diagnostic_export','query_failed','Delivery not acknowledged')
            return
        self.outbox.ack([r['event_id'] for r in batch]);self.delivered+=len(batch)
        d.capability('diagnostic_export','available',adapter='authenticated-http')
        from diagnostics.contract import utc_now
        d.capabilities['diagnostic_export']['last_success']=utc_now()

    def run(self):
        try:
            while not self.stop_event.is_set():
                try:self.flush()
                except Exception:self.failures+=1
                self.stop_event.wait(1)
        finally:
            if self.outbox:self.outbox.close()

    def start(self):
        self.thread=Thread(target=self.run,name='aiops-export',daemon=True);self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=2.5)


def start_exporter(runtime):
    if not runtime.diagnostics.enabled:return None
    previous=getattr(runtime,'diagnostic_exporter',None)
    if previous and previous.thread and previous.thread.is_alive():return previous
    url=os.getenv('AIOPS_SDK_DIAGNOSTICS_URL','');token=os.getenv('AIOPS_SDK_DIAGNOSTICS_TOKEN','')
    cfg=runtime.config
    if cfg and getattr(cfg,'observed_name',''):
        url=url or cfg.registration_url.removesuffix('/v1/observed/register')+'/v1/observations/batches'
        token=os.getenv(cfg.registration_token_env,'')
    if not url or not token:
        runtime.diagnostics.capability('diagnostic_export','unconfigured','Endpoint/token not configured')
        return None
    path=Path(os.getenv('AIOPS_SDK_DIAGNOSTICS_BUFFER','.run/diagnostic-outbox'))
    try:
        from hashlib import sha256
        from .system_scope import configured,buffer_key
        name=buffer_key(runtime) if configured(runtime) else sha256(runtime.service_name.encode()).hexdigest()[:24]
        exporter=Exporter(runtime,url,token,path/(name+'.sqlite3'))
        exporter.start();runtime.diagnostic_exporter=exporter
        return exporter
    except Exception:
        runtime.diagnostics.capability('diagnostic_export','query_failed','Exporter initialization failed')
        return None
