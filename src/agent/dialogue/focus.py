"""Reuse a unique explicitly named original symptom; never select by candidate rank."""
import re
from .contracts import snapshot,metadata,task_id

KINDS={'HTTP_REQUEST_CANCELLED':r'取消|中断|cancel',
       'HTTP_REQUEST_TIMEOUT':r'超时|timeout', 'DB_DEADLOCK':r'死锁|deadlock',
       'THREAD_POOL_REJECTED':r'线程池.{0,8}(满|拒绝)|工作池.{0,8}拒绝',
       'SQL_SLOW':r'慢\s*SQL|SQL.{0,8}(慢|耗时)',
       'POOL_ACQUIRE_TIMEOUT':r'连接.{0,8}(获取|等待).{0,8}超时'}


def matches(problems,text):
    kinds=[k for k,v in KINDS.items() if k in text or re.search(v,text,re.I)]
    endpoint=re.search(r'\b(?:GET|POST|PUT|PATCH|DELETE|HEAD)\s+/[^\s，。；;（）()]+',text)
    if not endpoint and len(kinds)!=1:return []
    return [p for p in problems if (not endpoint or p.get('endpoint')==endpoint[0])
            and (len(kinds)!=1 or p['error_type']==kinds[0])]


def described(text):
    return bool(re.search(r'\b(?:GET|POST|PUT|PATCH|DELETE|HEAD)\s+/',text) or
                any(k in text or re.search(v,text,re.I) for k,v in KINDS.items()))


def prefer(rows,request,decision,names):
    if decision.problem_id or decision.candidate_id:return
    scoped=[b for b in rows if not decision.task_id or decision.task_id in (task_id(b),b['incident_id'])]
    if not scoped:return
    body=scoped[0];ps=snapshot(body)['problems']
    from .relations import RELATION,focal
    if RELATION.search(request.message):
        target=focal(body)
        named=[q['service'] for q in ps if q['service'] in request.message]
        if target and (not named or target['service'] in named or re.search('这个|当前|该问题|它',request.message)):
            decision.problem_id=target['problem_id'];decision.task_id=task_id(body);return
    explicit=[n for n in names if re.search(r'(?<![\w.-])'+re.escape(n)+r'(?![\w.-])',request.message)]
    ps=[p for p in ps if not explicit or p['service'] in explicit]
    current=matches(ps,request.message)
    if described(request.message):
        if len(current)==1:
            decision.problem_id=current[0]['problem_id'];decision.task_id=task_id(body)
        return
    if not current:
        saved=metadata(body).get('problem_id')
        current=[p for p in ps if p['problem_id']==saved] if saved else []
    if not current:
        symptom=(body.get('slots') or {}).get('symptom') or ''
        named=[n for n in names if re.search(r'(?<![\w.-])'+re.escape(n)+r'(?![\w.-])',symptom)]
        current=matches([p for p in ps if not named or p['service'] in named],symptom)
    if len(current)==1:
        decision.problem_id=current[0]['problem_id'];decision.task_id=task_id(body)
