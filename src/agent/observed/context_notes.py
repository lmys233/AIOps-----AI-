"""Persist scoped middle/far memory notes without expanding the API service shell."""
from .context import session_key


async def note(service,request,body):
    from ..api.service import _append_note,_budget_of,CONTEXT_TURNS
    from ..orchestration.context_prompt import context_from_turns,summarize_with_gateway
    session=getattr(service._orchestrator,'session',None);writer=getattr(service.sessions,'index_put',None)
    if session is None or writer is None or not request.session_id:return
    try:
        turns=list(await session.turns(request.session_id,limit=CONTEXT_TURNS))
        result,page,_=await context_from_turns(turns,budget=_budget_of(body),session_id=request.session_id,
            summarize=summarize_with_gateway(getattr(service._router,'gateway',None)))
    except Exception as exc:
        _append_note(body,'上下文分层未生效（'+type(exc).__name__+'）→ 本次没有落库，不影响答案');return
    try:
        if result.summary:service.sessions.summary_put(session_key(request.session_id),result.summary)
        if page:writer(session_key(request.session_id),page)
    except Exception as exc:_append_note(body,'上下文分层落库失败（'+type(exc).__name__+'）')
