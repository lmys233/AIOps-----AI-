"""Bounded in-memory capture, once per LogRecord; no network or disk in processors."""
from datetime import datetime,timezone
from uuid import uuid4


def capture(runtime,data,method):
    if not getattr(runtime.config,'observed_name',''):return
    record=data.get('_record')
    if record is not None:
        if getattr(record,'_aiops_captured',False):return
        record._aiops_captured=True
    level=str(data.get('level') or (getattr(record,'levelname',None) if record else None) or method).upper()
    if level=='WARN':level='WARNING'
    payload={'level':level,'msg':str(data.get('msg') or data.get('event') or '')[:8000],
        'error_type':str(data.get('error_type') or '')[:128]}
    if level in ('ERROR','FATAL','CRITICAL'):payload['outcome']='exception'
    at=datetime.fromtimestamp(record.created,timezone.utc).isoformat() if record else None
    runtime.diagnostics.emit('application_log',payload,'sdk_log',occurred_at=at)
