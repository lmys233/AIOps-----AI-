"""Opt-in read-only observation of a checked Uvicorn shutdown cancel branch."""
from contextvars import ContextVar
import functools
import inspect
import logging
import os
from pathlib import Path
from uuid import uuid4
from .cancellation import REQUESTS,operation

CONTEXT=ContextVar('aiops_uvicorn_shutdown',default=None)
MESSAGE='Cancel %s running task(s), timeout graceful shutdown exceeded'


class ShutdownHandler(logging.Handler):
    def emit(self,record):
        context=CONTEXT.get()
        if not context or record.msg!=MESSAGE or record.funcName!='shutdown':return
        server,path=context;runtime=None
        try:
            if Path(record.pathname).resolve()!=path:return
            source=uuid4().hex
            for task in list(server.server_state.tasks)[:1024]:
                item=REQUESTS.get(task)
                if not item or task.done():continue
                runtime=item['runtime']
                state=item['state'];count=task.cancelling()
                row=runtime.diagnostics.emit('request_shutdown_cancel',dict(adapter='uvicorn_cancel_branch',
                    source_record=source,cancel_count_before=count,operation=operation(state['operation']),
                    request_started_at=state['request_started_at']), 'sdk_lifecycle',item['correlation'])
                if row:state['shutdown_marker']=dict(event_id=row['event_id'],cancel_count_before=count)
        except Exception:
            if runtime is not None:runtime.diagnostics.dropped+=1


def patch(runtime):
    if os.getenv('AIOPS_SDK_UVICORN_SHUTDOWN_OBSERVE','off').lower() not in ('on','1','true'):
        runtime.diagnostics.capability('request_shutdown','unconfigured','Optional framework observer disabled');return
    try:
        import uvicorn.server as module
        original=module.Server.shutdown
        checked=getattr(original,'__aiops_original__',original)
        source=inspect.getsource(checked);path=Path(inspect.getfile(checked)).resolve()
        if (MESSAGE not in source or 'for t in self.server_state.tasks:' not in source or
            't.cancel(msg="Task cancelled, timeout graceful shutdown exceeded")' not in source):
            raise ValueError('unsupported shutdown implementation')
        if not getattr(original,'__aiops_shutdown__',False):
            @functools.wraps(original)
            async def observe(server,*args,**kwargs):
                token=CONTEXT.set((server,path))
                try:return await original(server,*args,**kwargs)
                finally:CONTEXT.reset(token)
            observe.__aiops_shutdown__=True;observe.__aiops_original__=original
            module.Server.shutdown=observe
            logging.getLogger('uvicorn.error').addHandler(ShutdownHandler())
        runtime.diagnostics.capability('request_shutdown','available',adapter='checked-uvicorn-shutdown-branch')
    except (ImportError,OSError,TypeError,ValueError):
        runtime.diagnostics.capability('request_shutdown','unsupported','Uvicorn cancellation branch cannot be verified')
