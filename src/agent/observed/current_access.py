"""Publish this run's actual SDK failures with actual times, alongside historical evidence."""
from .context import current
from .attempts import store_of
from .access_agent import as_event
from ..diagnostics.evidence import from_event


async def publish(state,board):
    scope=current()
    if not scope:return
    present={e.evidence_id for e in state.evidence};items=[]
    for summary in scope.attempts:
        if summary.get('transport')!='sdk_direct' or not summary.get('sent') or summary.get('error')=='none':continue
        try:detail=await store_of(scope).detail(scope.tenant,scope.observed_id,summary['attempt_id'])
        except Exception:continue
        row=as_event(detail['body']);item=from_event(row,'sdk_access_agent',row['raw_ref'])
        if item.evidence_id not in present:items.append(item);present.add(item.evidence_id)
    if items:
        await board.publish(state.incident_id,'sdk_access_agent',items)
        from ..agents.base import AgentOutcome
        state.outcomes.append(AgentOutcome(role='sdk_access_agent',evidence=items))
