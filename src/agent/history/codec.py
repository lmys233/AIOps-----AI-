"""完整页面档案与有界模型上下文之间的转换。"""
from datetime import datetime,timezone
from ..memory.stores import ConversationTurn
from ..memory.session import _answer_fields,_tool_fields,_steps_fields
from ..memory.answer_snapshot import snapshot_of


def payload(request,body,details):
    return {'user_message':stored_message(request,body),'status':body.get('status',''),
        'incident_id':body.get('incident_id',''),'at':datetime.now(timezone.utc).isoformat(),
        'answer':(body.get('explanation') or {}).get('answer') or {},
        'response':body,'tools':details.get('tools',[]),'steps':details.get('steps',[]),
        'source':'archive','complete':not details.get('overflow',False)}


def stored_message(request,body):
    from ..diagnostics.ingestion import redact
    import json
    d=(body.get('explanation') or {}).get('dialogue') or {}
    if d.get('action')!='submit_evidence':
        pending=d.get('pending_target') or {}
        return pending['request_text'] if pending.get('action')=='submit_evidence' else request.message
    material=(d.get('submissions') or [{}])[-1]
    clean=material.get('content',redact(request.message))
    return '用户补充资料（脱敏留存）：\n'+(json.dumps(clean,ensure_ascii=False) if isinstance(clean,dict) else str(clean))


def as_turn(row):
    answer=row.get('answer',{});summary,lines,kind=_answer_fields(answer)
    response=row.get('response') or {};slots=response.get('slots') or {}
    return ConversationTurn(turn=int(row['turn']),user_message=row.get('user_message',''),
        response_digest=summary[:200],service_names=tuple(slots.get('service_names') or row.get('service_names') or []),
        at=row['at'],answer_summary=summary,answer_rows=lines,answer_kind=kind,
        answer_advisor_status=answer.get('advisor_status',''),
        answer_snapshot=snapshot_of(answer,(response.get('explanation') or {}).get('attribution_v2')),
        tools=_tool_fields(row.get('tools')),steps=_steps_fields(row.get('steps')))


def full_view(row):
    answer=row.get('answer') or ((row.get('response') or {}).get('explanation') or {}).get('answer') or {}
    return dict(row,summary=answer.get('summary') or '')
