"""Request-local alert provenance, copied into the same immutable diagnostic archive."""
from contextvars import ContextVar
from contextlib import contextmanager
from copy import deepcopy

CURRENT=ContextVar('aiops_alert_provenance',default=None)


@contextmanager
def use(metadata):
    token=CURRENT.set(metadata)
    try:yield
    finally:CURRENT.reset(token)


def stamp(body):
    value=CURRENT.get()
    if value:
        ex=body.setdefault('explanation',{});ex['alert']=deepcopy(value)
        ex['alert_ref']={k:value.get(k) for k in ('source','receipt_id','received_at','evidence_role')}
        ex['alert_ref']['service']=(value.get('labels') or {}).get('service')
