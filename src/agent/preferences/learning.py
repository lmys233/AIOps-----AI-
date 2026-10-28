"""Bounded abstraction rules: explicit feedback has precedence over weak likes."""
import re
from ..common.tokens import estimate_tokens
from .models import Profile,Preference,Experience,Support
from .store import digest,raw

STYLE={
 'concise':'回答先给结论，减少重复；必要依据、风险和未知仍保留。',
 'detailed':'在资料支持的范围内解释判断依据、步骤与限制，必要时给出可核对的明细。',
 'plain_language':'优先用通俗中文解释，首次使用专业术语时说明其含义。',
 'examples':'资料支持时提供简短例子；假设例子须标明假设，不能当作当前系统的事实。',
 'structured':'使用清晰的短段落或必要的表格组织回答。',
 'explain_gaps':'证据不足时，说明各未排除候选需要的数据、用途和获取方式。',
 'evidence_first':'结论旁给出可回查的证据及其限制。',
}
REASON={'too_long':'concise','unclear':'structured','missing_evidence':'evidence_first',**{k:k for k in STYLE}}


def supported(item,latest):
    return [s for s in item.get('support',[]) if s['feedback_id'] in latest and
            latest[s['feedback_id']].get('vote') is not None and
            latest[s['feedback_id']]['revision']==s['revision']]


def statistics_of(refs,latest):
    states=[latest[r['feedback_id']] for r in refs]
    return {'feedback_counts':{vote:sum(s.get('vote')==vote for s in states) for vote in ('up','down')},
            'last_supported_at':max((s.get('updated_at') or '' for s in states),default='') or None}


def effective(profile,latest,scope,capability_hash,*,experiences=True):
    out={'preferences':[],'experiences':[]}
    for item in profile.get('preferences',[]):
        refs=supported(item,latest)
        if refs:out['preferences'].append(dict(item,support=refs,**statistics_of(refs,latest)))
    for item in profile.get('experiences',[]) if experiences else []:
        refs=supported(item,latest)
        if refs and all(item.get(k)==scope.get(k) for k in ('observed_id','env','intent','family')) and item.get('capability_hash')==capability_hash:
            out['experiences'].append(dict(item,support=refs,**statistics_of(refs,latest)))
    out['preferences']=out['preferences'][:5];out['experiences']=out['experiences'][:2]
    while estimate_tokens(raw(out))>2000:
        (out['experiences'] if out['experiences'] else out['preferences']).pop()
    return out


def episode_source(row,feedback,capability_hash,tool_names=()):
    response=row.get('response') or {};ex=response.get('explanation') or {}
    answer=ex.get('answer') or {};tools=row.get('tools') or row.get('steps') or []
    chain=[]
    for tool in tools[:80]:
        if not isinstance(tool,dict):continue
        name=tool.get('tool') or tool.get('name') or ''
        if name not in tool_names:continue
        if not re.fullmatch(r'[a-z][a-z0-9_]{1,63}',name):continue
        chain.append({'tool':name,'status':tool.get('status') or ('done' if tool.get('ok') is True else 'failed' if tool.get('ok') is False else 'unknown'),
                      'result':str(tool.get('summary') or tool.get('result') or '')[:300]})
    slots=response.get('slots') or {};intent=response.get('intent') or {}
    quality=((answer.get('knowledge') or {}).get('quality') or {}).get('status')
    eligible=(response.get('status') in ('concluded','answered') and 'incorrect' not in feedback.get('reason_codes',[]) and
              (intent.get('name')!='KNOWLEDGE_QA' or quality in ('passed','repaired')))
    return {'question':str(row.get('user_message',''))[:2400],
            'answer':str(answer.get('summary') or response.get('report') or row.get('summary') or '')[:2400],
            'status':response.get('status'),'experience_eligible':eligible,
            'feedback':{k:feedback.get(k) for k in ('vote','reason_codes','comment','revision','feedback_id')},
            'scope':{'observed_id':slots.get('observed_id') or (ex.get('observed_system') or {}).get('observed_id'),
                     'env':slots.get('env') or 'prod','intent':intent.get('name',''),'family':problem_family(ex)},
            'capability_hash':capability_hash,'tools':chain,'saved_answer_ref':feedback['answer_ref']}


def problem_family(ex):
    problems=(ex.get('attribution_v2') or {}).get('problems') or []
    families=sorted(set(p.get('error_type') or p.get('family') or '' for p in problems))
    return '|'.join(families)[:160]


def abstract(episodes,old,latest):
    """Canonical style contracts and real tool names prevent model-added privileges/facts."""
    collected={};experience={}
    for item in old.get('preferences',[]):
        if item['key'] in STYLE:collected[item['key']]=supported(item,latest)
    for item in old.get('experiences',[]):
        refs=supported(item,latest)
        if refs:experience[item['key']]=dict(item,support=refs)
    for episode in episodes:
        f=episode['feedback'];ref={'feedback_id':f['feedback_id'],'revision':f['revision']}
        if latest.get(f['feedback_id'],{}).get('revision')!=f['revision'] or f.get('vote') is None:continue
        for reason in f.get('reason_codes',[]):
            key=REASON.get(reason)
            if key:
                if key in ('concise','detailed'):collected.pop('detailed' if key=='concise' else 'concise',None)
                collected.setdefault(key,[]).append(ref)
        # Freeform comments may suggest a canonical style only after semantic summarization.
        if f.get('comment'):
            for key in episode.get('learned',{}).get('preferences',[]):
                witness=episode.get('learned',{}).get('witnesses',{}).get(key,'')
                if key in STYLE and len(witness)>=4 and witness in f['comment']:
                    if key in ('concise','detailed'):collected.pop('detailed' if key=='concise' else 'concise',None)
                    collected.setdefault(key,[]).append(ref)
        eligible=episode.get('experience_eligible',episode.get('scope',{}).get('intent')!='KNOWLEDGE_QA')
        if eligible and 'incorrect' not in f.get('reason_codes',[]) and f.get('vote')=='up' and 'effective_process' in f.get('reason_codes',[]) and episode.get('status') in ('concluded','answered'):
            chain=[t['tool'] for t in episode['tools'] if t.get('status') in ('ok','completed','success','done','succeeded')]
            if not chain:continue
            scope=episode['scope'];key='experience_'+digest([scope,episode['capability_hash'],chain])[:20]
            previous=experience.get(key,{})
            experience[key]={'key':key,**scope,'capability_hash':episode['capability_hash'],
                'when':'仅在同范围、同问题族且当前证据缺口匹配时参考；历史结果不证明现场事实。',
                'steps':list(dict.fromkeys(chain))[:6],'stop':'无新证据、能力不可用、预算耗尽或需要用户材料时停止。',
                'support':(previous.get('support',[])+[ref])[-20:]}
    prefs=[{'key':k,'instruction':STYLE[k],'support':list({r['feedback_id']:r for r in refs}.values())[-20:]}
           for k,refs in collected.items() if refs]
    for item in prefs+list(experience.values()):item.update(statistics_of(item['support'],latest))
    profile=Profile(preferences=[Preference(**x) for x in prefs[-16:]],experiences=[Experience(**x) for x in list(experience.values())[-8:]])
    while estimate_tokens(profile.model_dump_json())>4096:
        (profile.experiences if profile.experiences else profile.preferences).pop(0)
    return profile.model_dump()


def partitions(rows,limit,overhead=2500):
    if limit<=overhead:raise ValueError('model window too small for preference policy')
    groups=[];group=[];size=0
    for row in rows:
        amount=estimate_tokens(raw(row))
        if amount>limit-overhead:raise ValueError('single episode exceeds partition window')
        if group and size+amount>limit-overhead:groups.append(group);group=[];size=0
        group.append(row);size+=amount
    if group:groups.append(group)
    return groups
