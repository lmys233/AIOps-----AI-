import re
from .text import metadata,terms

PRONOUN=re.compile(r'它|这个|那个|上述|前面|该问题|这种|这类')


def strip_window(text):
    return re.sub(r'[（(]时间窗\s*\d+[smhd][）)]','',text).strip()


def slots(text):
    meta=metadata(text)
    exact_entities=re.findall(r'(?:表|字段|对象|函数|错误码|SQLSTATE)\s*[=:：]?\s*`?([A-Za-z_][A-Za-z0-9_./:-]{1,100}|[0-9][A-Za-z0-9]{4})',text,re.I)
    exact_entities.extend(code for code in re.findall(r'(?<![A-Za-z0-9_])[A-Z0-9_]{4,32}(?![A-Za-z0-9_])',text)
                          if any(c.isalpha() for c in code) and any(c.isdigit() for c in code))
    return {'entities':meta['entities'],'exact_entities':list(dict.fromkeys(exact_entities))[:30],'dates':meta['times'],
            'exact_codes':meta['code_terms'],'keywords':terms(text,32)}


def history_items(turns,text):
    items=[]
    for turn in turns[-4:]:
        value=turn if isinstance(turn,dict) else vars(turn)
        message=strip_window(str(value.get('user_message') or ''))
        if message and message!=text:
            items.append({'message':message[:1000],'summary':str(value.get('answer_summary') or '')[:1500],
                          'services':list(value.get('service_names') or [])[:10],'turn':value.get('turn')})
    return items


def rewrite(text,turns):
    original=strip_window(text);items=history_items(turns,original);parsed=slots(original)
    result={'original_query':original,'rewritten_query':original,'slots':parsed,
            'rewrite_source':'unchanged','history_turns':[],'ambiguous':False}
    elliptical=bool(re.match(r'^(怎么|如何|为什么|还有|步骤|流程|处理步骤|处理流程|预防方法|原理|条件)',original))
    needs=bool(PRONOUN.search(original)) or (len(original)<12 and elliptical)
    if not needs or not items:return result
    recent=items[-1];services=recent['services']
    multiple=len(services)>1 or bool(re.search(r'与|以及|还是|\band\b|\bor\b',recent['message'],re.I))
    if PRONOUN.search(original) and multiple:
        return {**result,'ambiguous':True,'choices':services,'reason':'最近一轮涉及多个对象，不能替用户决定指代'}
    # Copy bounded context verbatim, rather than inventing a subject from a short question.
    subject=re.sub(r'^(?:请|介绍|解释|什么是|说明)+','',recent['message']).strip()
    subject=re.split(r'是什么|是什么原因|有哪些|如何|怎么|有什么',subject)[0].strip('？?。 ')[:250]
    resolved=re.sub(r'它|这个问题|那个问题|这种问题|该问题|上述问题',subject,original) if subject and not parsed['exact_codes'] else original
    # Put the current question first: encoder token truncation must not hide the new purpose.
    context='本轮问题：'+resolved+'；最近讨论：'+recent['message'][:300]
    if recent['summary']:context+='；上下文摘要：'+recent['summary'][:500]
    result.update(rewritten_query=context,resolved_subject=subject,
                  rewrite_source='recent_same_session_context',history_turns=[recent['turn']])
    return result
