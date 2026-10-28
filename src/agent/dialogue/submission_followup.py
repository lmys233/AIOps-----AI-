"""User material can select a registered check, never supply its truth value."""
from .contracts import Decision
import re


def refresh_request(material,problem,text):
    """Explicit top-level refresh request can read the *old* authorized scope.

    Plain claims remain unverified. Instructions inside pasted logs/JSON or
    mismatching structured records never trigger this path.
    """
    if material.get('verification')!='unverified' or material.get('truncated') or material.get('issues'):return None
    if not isinstance(material.get('content'),str):return None
    command=re.split(r'[\n：:{`]',text,maxsplit=1)[0]
    if re.search(r'(?:不|不要|不用|无需|别).{0,4}(?:核查|核对|验证|复核|查询)',command):return None
    if not re.search(r'(?:请|继续|重新|再|先).{0,24}(?:核查|核对|验证|复核|查询)|\b(?:please|recheck|verify)\b',command,re.I):return None
    if not problem.get('problem_id') or not problem.get('service') or not problem.get('time_range'):return None
    return Decision(action='verify_target',reason='按本轮明确复核请求读取原问题的登记来源；不把用户文字作为证据')


def choose(material,problem):
    content=material.get('content')
    if material.get('verification')!='unverified' or material.get('truncated') or not isinstance(content,dict):return None
    if content.get('service')!=problem['service'] or content.get('env')!=problem.get('env'):return None
    anchors=[k for k in ('request_id','query_fingerprint','object_name') if problem.get(k)]
    if anchors and not any(content.get(k)==problem[k] for k in anchors):return None
    if not anchors:
        from datetime import datetime
        if not (problem.get('instance') and problem.get('boot_id')):return None
        try:
            if datetime.fromisoformat(content.get('occurred_at','').replace('Z','+00:00'))!=datetime.fromisoformat(str(problem.get('first_seen','')).replace('Z','+00:00')):return None
        except (ValueError,TypeError):return None
    if any(content.get(k) and content[k]!=problem[k] for k in anchors):return None
    for k in ('instance','boot_id'):
        if problem.get(k) and content.get(k)!=problem[k]:return None
    signals=content.get('signals') or [content.get('signal')]
    if not isinstance(signals,list) or len(signals)>8 or not all(isinstance(s,str) for s in signals):return None
    candidates=[c for c in problem['candidates'] if c['status']!='excluded' and
        any(r['signal'] in signals for r in c['requirements'])]
    if content.get('candidate_id'):candidates=[c for c in candidates if c['candidate_id']==content['candidate_id']]
    # An explicit candidate or a uniquely matching candidate binds the query.
    if len(candidates)!=1:return None
    return Decision(action='verify_target',candidate_id=candidates[0]['candidate_id'],
        reason='资料与当前问题的服务、环境和事件关联匹配；只核查已有候选，聊天值不作事实')
