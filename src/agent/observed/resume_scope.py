"""Explicit or immutable session scope is restored before reading pending conversation state."""
from .context import enabled,use
from .chat import resolve
from ..api.requests import ChatRequest,ChatContext,ChatUser
from ..api.errors import ResumeError


async def run(service,payload,trace_id):
    if not enabled() and not payload.get('observed_id'):
        return await service.resume(payload,trace_id=trace_id)
    try:
        values=dict(payload.get('context') or {})
        if payload.get('observed_id'):values['observed_id']=payload['observed_id']
        context=ChatContext.model_validate(values)
        request=ChatRequest(session_id=str(payload.get('session_id') or ''),message='恢复任务上下文',
            user=ChatUser(id=str(payload.get('user_id') or 'human:unknown')),context=context)
        scope=await resolve(service,request)
    except Exception as exc:
        raise ResumeError(422,'INVALID_PARAMS','恢复目标的系统归属未能核实；请使用原系统工作台') from exc
    with use(scope):return await service.resume(payload,trace_id=trace_id)
