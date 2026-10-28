"""Transport/history/cache details isolated from application task transitions."""
from ..history.codec import payload


def persisted_payload(request,body,details):return payload(request,dict(body),details)


def refusal(request,trace_id,exc):
    from ..api.contract_body import empty_contract_body
    body=empty_contract_body(request.session_id,trace_id=trace_id,turn_id=request.turn_id,
        status='clarify' if isinstance(exc,(ValueError,PermissionError)) else 'failed',intent_name='INCIDENT_RCA',write_allowed=False)
    reason=str(exc) if isinstance(exc,(ValueError,PermissionError)) else '任务控制未完成：'+type(exc).__name__+'；未绕过任务状态执行'
    body['report']=reason;body['degraded'].append(reason)
    body['explanation']['answer']={'source':'task_control','summary':reason,'details':[],'details_kind':'table'}
    return body


async def invalidate(service,sid,body,annotate=True):
    cache=getattr(getattr(service,'history',None),'cache',None)
    if cache:
        from ..observed.context import session_key
        try:
            await cache.invalidate(sid)
            if session_key(sid)!=sid:await cache.invalidate(session_key(sid))
        except Exception as exc:
            service.history.dirty.add(session_key(sid))
            if annotate:body['degraded'].append('页缓存失效失败：'+type(exc).__name__+'；历史页需从数据库回源')
            else:
                from ..api.constants import logger
                logger.warning('任务/历史已提交，页缓存失效失败：%s',type(exc).__name__)


async def cache_result(service,body):
    try:await service._store_incident(body)
    except Exception:
        from ..api.constants import logger
        logger.exception('任务/历史已在 PG 提交，结果缓存更新失败：%s',body['incident_id'])
