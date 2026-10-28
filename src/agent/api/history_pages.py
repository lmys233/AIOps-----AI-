"""历史分页：仅旧缓存确有正文才迁入；空档案不伪造历史。"""
from datetime import datetime,timezone
from ..history.codec import full_view


class InvalidHistoryCursor(ValueError):pass


def cursor(value):
    if value is None:return None
    if len(str(value))>19 or not str(value).isdigit() or not 0<int(value)<2**63:
        raise InvalidHistoryCursor('历史游标必须为有效正整数')
    return int(value)


async def page_body(service,record,before=None):
    before=cursor(before);history=service.history
    data=await history.page(record.session_id,before)
    if before is None and not data['turns']:
        if await import_legacy(service,record.session_id):data=await history.page(record.session_id)
    if before is None and not data['turns']:
        from .sessions import _recent_turns
        rows,notes=await _recent_turns(service,record.session_id)
        return {'session':record.to_dict(),'turns':rows,'degraded':data['degraded']+notes,
                'has_more':False,'next_cursor':None,'page_size':20,'source':'ledger' if rows else 'database'}
    if any(not r.get('complete',False) for r in data['turns']):
        data['degraded']=data['degraded']+['该页含旧缓存迁入或步骤超限的记录；保留实际取得的内容，早期截断无法补回']
    return dict(data,session=record.to_dict(),turns=[full_view(r) for r in data['turns']])


async def import_legacy(service,sid):
    from .sessions import _view
    memory=getattr(service.orchestrator,'session',None)
    working=getattr(memory,'working',None);base=getattr(working,'base',working)
    if not base:return False
    try:turns=await base.turns(sid,limit=20)
    except Exception:return False
    meta=list(service.sessions.turn_meta(sid,limit=20));rows=_view(turns,meta)
    for row,turn in zip(rows,turns):
        if not row.get('user_message'):continue
        at=row.get('at')
        try:
            if str(at).replace('.','',1).isdigit():at=datetime.fromtimestamp(float(at),timezone.utc).isoformat()
            datetime.fromisoformat(at)
        except (TypeError,ValueError):continue
        row.update(at=at,complete=False,source='legacy_cache',service_names=list(turn.service_names))
        tid='legacy:'+str(row.get('incident_id') or at)
        await service.history.save(sid,tid,row)
    return bool(rows)
