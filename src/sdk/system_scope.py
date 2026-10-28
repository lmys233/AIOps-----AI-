"""Attach only authenticated registration identity; no business headers are trusted."""
from copy import deepcopy
from diagnostics.observed import scoped_identity
from diagnostics.contract import validate_event


def attach(runtime,identity):
    checked=scoped_identity(identity);d=runtime.diagnostics
    with d.lock:
        if any(d.identity[k]!=checked[k] for k in ('service','env','cluster','instance','boot_id')):
            raise ValueError('registered runtime identity changed')
        if d.identity.get('observed_id') not in (None,checked['observed_id']):
            raise ValueError('runtime cannot switch observed systems')
        d.identity=checked
        for row in d.events:
            if all(row['identity'].get(k)==checked[k] for k in ('service','env','cluster','instance','boot_id')):
                row.update(schema_version=2,identity=dict(checked));validate_event(row)
    runtime.observed_identity=checked


def configured(runtime):return bool(runtime and getattr(runtime.config,'observed_name',''))


def identity(runtime):return getattr(runtime,'observed_identity',None) if runtime else None


def export_ready(runtime):return not configured(runtime) or identity(runtime) is not None


def buffer_key(runtime):
    import hashlib,os
    i=runtime.diagnostics.identity;cfg=runtime.config
    key=[i['tenant'],getattr(cfg,'observed_name',''),runtime.service_name,i['instance']]
    token=os.getenv(getattr(cfg,'registration_token_env','AIOPS_SDK_REGISTRATION_TOKEN'),'')
    key.append(hashlib.sha256(token.encode()).hexdigest())
    return hashlib.sha256('|'.join(key).encode()).hexdigest()[:32]
