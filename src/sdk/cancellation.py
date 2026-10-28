"""Request-local observations. Hooks only report an existing cancel result."""
import asyncio
from contextvars import ContextVar
from weakref import WeakKeyDictionary
from uuid import uuid4
from diagnostics.contract import utc_now
from diagnostics.cancellation import OPERATIONS
from .diagnostic_state import current_correlation

STATE=ContextVar('aiops_cancellation_state',default=None)
OPERATION=ContextVar('aiops_cancellation_operation',default='application')
REQUESTS=WeakKeyDictionary()


def operation(value):return value if value in OPERATIONS else 'unknown'


def begin(runtime,state):
    state.update(request_started_at=utc_now(),operation='application',deadline_errors=[])
    state['operation_token']=OPERATION.set('application')
    task=asyncio.current_task()
    try:
        if task is not None and len(REQUESTS)<1024:
            state['previous_request']=REQUESTS.get(task)
            REQUESTS[task]=dict(runtime=runtime,state=state,correlation=current_correlation())
    except Exception:runtime.diagnostics.dropped+=1
    return STATE.set(state)


def end(token):
    state=STATE.get()
    task=asyncio.current_task()
    if task is not None:
        previous=state.get('previous_request') if state is not None else None
        if previous:REQUESTS[task]=previous
        else:REQUESTS.pop(task,None)
    if state is not None:OPERATION.reset(state['operation_token'])
    STATE.reset(token)


def disconnect(runtime,state):
    from .middleware_http import route_template,bounded_method
    scope=state['scope']
    payload=dict(operation=operation(OPERATION.get()),received_after_response=state['completed'],
        route=runtime.diagnostics.bounded_route(route_template(scope,runtime)),method=bounded_method(scope.get('method')),
        request_started_at=state['request_started_at'])
    row=runtime.diagnostics.emit('request_disconnect',payload,'sdk_http',current_correlation())
    if row:state['disconnect_event_id']=row['event_id']


def observe_cancel_result(task,accepted,*,origin='unknown',source_record=None):
    """Optional framework callback AFTER its own task.cancel(); never calls cancel."""
    item=REQUESTS.get(task)
    if item is None:return None
    runtime=item['runtime'];state=item['state']
    try:
        row=runtime.diagnostics.emit('request_cancel',dict(accepted=accepted,origin=origin,
            adapter='runtime_hook',source_record=source_record or uuid4().hex,
            trigger_event_id=state.get('disconnect_event_id') if origin=='client_disconnect' else None,
            operation=operation(state['operation']),request_started_at=state['request_started_at']),
            'sdk_runtime',item['correlation'])
        if row and accepted:state['cancel_trigger_event_id']=row['event_id']
        return row
    except Exception:runtime.diagnostics.dropped+=1;return None


def failure_phase(error,name):
    state=STATE.get()
    if state is not None:state['phase_error']=(error,name)


def terminal(state,exc):
    out={};errors=[];cursor=exc
    for _ in range(4):
        if cursor is None or any(cursor is e for e in errors):break
        errors.append(cursor);cursor=cursor.__cause__
    phase=state.get('phase_error')
    out['operation']=operation(phase[1] if phase and any(phase[0] is e for e in errors) else OPERATION.get())
    if isinstance(exc,asyncio.CancelledError):
        out['cancel_count']=asyncio.current_task().cancelling()
        if state.get('cancel_trigger_event_id'):out['cancel_trigger_event_id']=state['cancel_trigger_event_id']
        marker=state.get('shutdown_marker')
        if marker and marker['cancel_count_before']==0 and out['cancel_count']==1 and exc.args==('Task cancelled, timeout graceful shutdown exceeded',):
            out['shutdown_trigger_event_id']=marker['event_id']
    for error,event_id in state['deadline_errors']:
        if any(error is e for e in errors):out['deadline_trigger_event_id']=event_id
    return out
