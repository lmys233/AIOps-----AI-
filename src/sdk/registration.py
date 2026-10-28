"""Optional bounded background registration. No platform modules imported."""
import hashlib,json,os
from pathlib import Path
from threading import Event,Thread
from urllib.request import Request,urlopen
from diagnostics.observed import registration,CAPABILITIES,system_id
from diagnostics.credentials import pull_token


def settings(doc,section,name,port,env):
    shared=doc.get('aiops',{}).get('registration',{})
    config=dict(shared,**section.get('registration',{}))
    observed=env.get('AIOPS_OBSERVED_NAME') or doc.get('observed-name') or config.get('observed-name') or ''
    return dict(observed_name=observed,registration_url=env.get('AIOPS_SDK_REGISTRATION_URL') or config.get('url',''),
        sdk_advertise_url=env.get('AIOPS_SDK_ADVERTISE_URL') or config.get('advertise-url',''),
        registration_token_env=config.get('token-env','AIOPS_SDK_REGISTRATION_TOKEN'),
        business_advertise_url=env.get('AIOPS_BUSINESS_ADVERTISE_URL') or config.get('business-advertise-url',''),
        active_probe=dict(section.get('active-probe',{})))


class Registrar:
    def __init__(self,runtime,transport=None,path=None):
        self.runtime=runtime;self.config=runtime.config;self.transport=transport or self.send
        self.stop=Event();self.thread=None;self.registered=False;self.attempts=0;self.last_error=''
        self.token=os.getenv(self.config.registration_token_env,'');self.identity=None
        key='|'.join([self.config.observed_name,runtime.service_name,runtime.diagnostics.identity['instance'],
            hashlib.sha256(self.token.encode()).hexdigest()])
        self.path=Path(path or Path('.run/sdk-registration')/(hashlib.sha256(key.encode()).hexdigest()+'.json'))

    def manifest(self):
        from . import __version__
        cfg=self.config;i=self.runtime.diagnostics.identity
        old=None
        if self.path.exists():
            if self.path.stat().st_size>16384:raise ValueError('registration state too large')
            old=json.loads(self.path.read_text(encoding='utf-8'))['observed_id'];system_id(old)
        enabled=['health','metrics','services']
        if self.runtime.diagnostics.enabled:enabled+=['capabilities','snapshot','probe_observations']
        value=dict(schema_version=1,observed_name=cfg.observed_name,observed_id=old,
            service=i['service'],env=i['env'],cluster=i['cluster'],instance=i['instance'],boot_id=i['boot_id'],
            base_url=cfg.sdk_advertise_url,sdk_version=__version__,capabilities=[
                dict(id=k,method='GET',path=CAPABILITIES[k],version=1,enabled=True) for k in enabled])
        if hasattr(self.runtime,'endpoint_inventory'):
            from diagnostics.endpoints import revision
            value.update(schema_version=2,endpoints=self.runtime.endpoint_inventory,
                endpoint_status=self.runtime.endpoint_status,endpoint_revision=revision(self.runtime.endpoint_inventory),
                business_url=cfg.business_advertise_url or cfg.sdk_advertise_url)
        return registration(value)

    def send(self,manifest):
        from diagnostics.observed import base_url
        # A registry endpoint is fixed; userinfo/query and arbitrary paths are forbidden.
        url=self.config.registration_url
        if not url.endswith('/v1/observed/register'):raise ValueError('invalid registration URL')
        base_url(url[:-len('/v1/observed/register')])
        req=Request(url,json.dumps(manifest).encode(),{'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        with urlopen(req,timeout=2) as r:return json.loads(r.read(16384))

    def once(self):
        self.attempts+=1
        if not self.token:raise ValueError('registration credential is not configured')
        m=self.manifest();result=self.transport(m);oid=system_id(result['observed_id'])
        if any(result.get(k)!=m[k] for k in ('service','instance','boot_id')):raise ValueError('registration response scope mismatch')
        if m['observed_id'] and oid!=m['observed_id']:raise ValueError('registration system changed')
        i=dict(self.runtime.diagnostics.identity,tenant=result['tenant'],observed_id=oid)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temp=self.path.with_suffix('.tmp');temp.write_text(json.dumps({'observed_id':oid}),encoding='utf-8');temp.replace(self.path)
        from .system_scope import attach
        attach(self.runtime,i)
        self.identity=i;self.registered=True;self.last_error=''
        self.runtime.diagnostics.capability('registration','available','Registered; current reachability not checked')
        self.runtime.diagnostics.emit('service_manifest',{'downstream':dict(self.config.downstream)},'sdk_runtime')
        return result

    def run(self):
        for n in range(8):
            if self.stop.is_set():return
            try:self.once();return
            except Exception as exc:
                self.last_error=type(exc).__name__
                self.runtime.diagnostics.capability('registration','query_failed','Registration unavailable: '+self.last_error)
            if self.stop.wait(min(10,.2*2**n)):return

    def start(self):
        if self.thread and self.thread.is_alive():return
        if self.registered:return
        self.thread=Thread(target=self.run,name='aiops-register',daemon=True);self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=2.5)

    def read_token(self):
        return pull_token(self.token,self.identity) if self.identity else ''


def start(runtime):
    cfg=runtime.config
    if not cfg or not getattr(cfg,'observed_name',''):return None
    previous=getattr(runtime,'registrar',None)
    if previous:return previous
    try:
        worker=Registrar(runtime);runtime.registrar=worker;worker.start();return worker
    except Exception as exc:
        runtime.diagnostics.capability('registration','query_failed',type(exc).__name__)
        return None
