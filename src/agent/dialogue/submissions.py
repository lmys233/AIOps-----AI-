"""Chat material remains untrusted; authenticated archives are separate."""
import hashlib
import json
import re
from datetime import datetime,timezone
from ..diagnostics.ingestion import redact
from ..common.enums import CHAT_MESSAGE_MAX
from .routing import strip_ui_window


def clean_text(text):
    raw=strip_ui_window(text[:CHAT_MESSAGE_MAX]);parsed=None;parse_error=False
    begin=raw.find('{')
    if begin>=0:
        try:parsed=json.loads(raw[begin:])
        except (ValueError,RecursionError):parse_error=True
    try:clean=redact(parsed if isinstance(parsed,dict) else raw)
    except ValueError:clean='资料嵌套超过上限，无法解析；未采用。';parse_error=True
    if isinstance(clean,str):
        # Even invalid JSON must not retain quoted credential values in a pending question.
        clean=re.sub(r'(?i)("[^"\n]*(?:password|passwd|pwd|secret|token|credential|dsn|authorization|api[_-]?key)[^"\n]*"\s*:\s*)"(?:\\.|[^"\\])*"',
                     lambda m:m[1]+'"***"',clean)
    return clean,parse_error

def receive(text,p,previous):
    clean,parse_error=clean_text(text)
    digest=hashlib.sha256(json.dumps([p['problem_id'],clean],sort_keys=True,ensure_ascii=False).encode()).hexdigest();items=list(previous or [])
    if any(i['digest']==digest or i.get('problem_id')==p['problem_id'] and i.get('content')==clean for i in items):return items,'相同资料已留存，本轮不重复加入。'
    if len(items)>=20:return items,'资料达到本任务 20 项上限；请先核实已有资料或明确开启新任务。'
    parsed=clean if isinstance(clean,dict) else None
    state='unverified';issues=['JSON 无法解析，按未核验文本保留'] if parse_error else []
    if isinstance(parsed,dict):
        for field in ('service','env','instance','boot_id','request_id'):
            if parsed.get(field) and p.get(field) and parsed[field]!=p[field]:issues.append(field+' 与当前问题不匹配')
        if parsed.get('occurred_at'):
            try:
                at=datetime.fromisoformat(parsed['occurred_at']);start,end=(datetime.fromisoformat(t) for t in p['time_range'])
                if at.tzinfo is None or not start<=at<=end:issues.append('时间/时区不在当前问题范围')
            except (ValueError,TypeError):issues.append('时间无法校验')
        if issues:state='scope_mismatch'
    items.append(dict(digest=digest,received_at=datetime.now(timezone.utc).isoformat(),
        problem_id=p['problem_id'],source='user_supplied',verification=state,
        content=clean,issues=issues,truncated=len(text)>CHAT_MESSAGE_MAX))
    return items,'资料已保存为未核验的用户说明；'+('；'.join(issues) if issues else '需要通过受权来源核实；不会将聊天正文直接作为原因确认的支持。')
