"""页面、记忆和导出共享的诊断快照，文案无需二次模型生成。"""
import json
import re
from .display_groups import groups, diagnosis, candidates, unique

STATE={'candidate':'待验证','confirmed':'已确认','excluded':'已排除'}
CHECK={'not_queried':'尚未查询','satisfied':'已满足','refuted':'可靠反证',
       'query_failed':'查询失败','insufficient_coverage':'覆盖不足'}


def rows(snapshot):
    out=[]
    localization=snapshot.get('source',{}).get('metric_localization') or {}
    if localization.get('ranked_services'):
        out.append({'label':'服务核查优先级（不是根因确认）','value':'；'.join(
            r['service']+'：'+', '.join(r['metrics'])+'；证据 '+', '.join(r.get('evidence_ids',[]))+'；原始引用 '+', '.join(r['raw_refs'])
            for r in localization['ranked_services'][:3])+'。'+localization['note']})
    if snapshot.get('source',{}).get('kind')=='previous_snapshot':
        out.append({'label':'诊断来源','value':'上一轮事故 '+snapshot['source']['incident_id']+'；未重新核查当前状态'})
    from ...observed.access_status import rows as access_rows
    if snapshot.get('source',{}).get('sdk_separated'):
        out.append({'label':'观测链路核验','value':observability_summary(snapshot)})
    else:out.extend(access_rows(snapshot))
    for members in groups(snapshot):
        out.append(diagnosis(members,previous_snapshot=snapshot.get('source',{}).get('kind')=='previous_snapshot'))
        complete=[dict(p,candidates=p['candidates']+snapshot.get('candidate_backlog',{}).get(p['problem_id'],[])) for p in members]
        out.extend(candidates(complete,STATE,CHECK))
        waiting=[c['candidate_id'] for p in members for c in snapshot.get('candidate_backlog',{}).get(p['problem_id'],[])]
        if waiting:out.append({'label':'待查队列 · '+members[0]['service']+' · '+members[0]['error_type'],
            'value':'因本轮候选/取证额度未安排；未安排不是排除：'+'、'.join(waiting)})
    labels={'unconfigured':'来源未配置','unsupported':'适配器未接入','invalid_source':'来源不可验证'}
    for gap in snapshot.get('capability_gaps',[]):
        out.append({'label':'能力缺口 · '+gap['service']+' · '+gap['tool'],
            'value':labels.get(gap['status'],gap['status'])+'（未调用）；'+gap['reason']+'；接入方式：'+gap['remedy']})
    out.append({'label':'诊断停止原因','value':snapshot.get('stop_reason','')})
    if snapshot.get('pending_problems'):
        out.append({'label':'预算内待查问题','value':'、'.join(snapshot['pending_problems'])})
    return out


def summary(snapshot):
    ps=snapshot.get('problems',[])
    from ...observed.access_status import summary as access_summary
    separated=snapshot.get('source',{}).get('sdk_separated')
    access=observability_summary(snapshot) if separated else access_summary(snapshot)
    if not ps:return ('本轮未取得可定位的业务故障样本；这不足以证明业务正常。 '+access) if separated else (access+' ' if access else '')+'请求窗口内未取得可定位的自身故障样本；这不足以证明系统正常。'
    confirmed=sum(p['cause']['status']=='confirmed' for p in ps)
    grouped=groups(snapshot)
    facts='；'.join(_group_summary(members) for members in grouped)
    count=(f'{len(ps)} 个问题' if len(grouped)==len(ps) else
           f'{len(grouped)} 类问题（{len(ps)} 个观测分组，分别核查）')
    unit='观测分组' if len(grouped)!=len(ps) else '问题'
    checks=snapshot.get('sdk_access_checks',[])
    window=('查询与本轮核查记录中发现' if any(not c['ok'] for c in checks) else
            '历史窗口内发现') if access else '窗口内发现'
    main=f'{window} {count}：{facts}。形成原因已确认 {confirmed} 个{unit}，其余保留候选与证据缺口。历史采样不能证明故障此刻仍在发生。'
    from .confidence import headline
    provisional=headline(snapshot) if snapshot.get('source',{}).get('confidence_version') else ''
    if provisional:main+=' '+provisional+'。'
    if snapshot.get('source',{}).get('confidence_version') and not confirmed and any(p.get('candidates') for p in ps):main+=' 你能补充下方候选所需的同事件原记录吗？我会据此继续核验并更新判断。'
    localization=snapshot.get('source',{}).get('metric_localization') or {}
    if not confirmed and localization.get('ranked_services'):
        main+=' 按指标采样变化，建议优先核查 '+ '、'.join(r['service'] for r in localization['ranked_services'][:3])+'；该排序不是已确认根因。'
    return main+' '+access if separated else (access+' ' if access else '')+main


def observability_summary(snapshot):
    checks=(snapshot.get('sdk_verification') or {}).get('checks',[])
    success=sum(c.get('state')=='verified' for c in checks)
    missing=sum(c.get('state')=='not_queried' for c in checks)
    failed=sum(c.get('state')=='failed' for c in checks)
    saved=sum(c.get('state')=='saved_verified' for c in checks)
    historical=len(snapshot.get('observability_problems',[]))
    if not checks:return 'SDK访问记录单独留存，不计入业务问题；当前链路核验没有可用结果。' if historical else '未发现需要强制核验的SDK访问错误。'
    if snapshot.get('source',{}).get('kind')=='previous_snapshot':return f'留存的SDK具体链路核验中 {success} 个成功、{failed} 个失败；保留原核查时刻，未重新确认当前状态。'
    if success==len(checks):return f'已用 web-query 核验 {success} 个具体SDK接口，当前均可达且身份匹配；历史访问错误已单独留存，不计入业务问题。'
    return f'SDK具体链路核验：{success} 个成功、{failed} 个失败、{missing} 个未执行、{saved} 个复用留存结果；相关数据获取可能受限，不能据此认定业务宕机。详情见工具调用记录。'


def _group_summary(members):
    p=members[0]
    text=p['service']+'：'+(p['mechanism']['statement'] or p['error_type'])
    if p.get('metric_name') and not p['mechanism']['statement']:
        text=p['service']+'：指标 '+p['metric_name']+' 在采样中变化，原因待核查'
    if p['error_type'].startswith('SDK_ACCESS_'):
        from ...observed.access_status import error_label
        text=p['service']+'：SDK 接口'+error_label(p['error_type'])
    qualifiers=unique(x.get('endpoint') or x.get('query_fingerprint') or '' for x in members)
    qualifier=' / '.join(x for x in qualifiers if x)
    if qualifier:text+='（'+qualifier+'）'
    if len(members)>1:text+=f'，{len(members)} 个观测分组'
    return text


def finish(body):
    ex=body.get('explanation',{})
    snap=ex.get('attribution_v2')
    if not snap:return
    answer=ex.setdefault('answer',{})
    answer['details']=rows(snap)+_additional_rows(answer.get('details') or [])
    head=str(answer.get('summary') or '')
    starts=[head.find(k) for k in ('本次给出','你选的方案') if k in head]
    if starts:head=head[min(starts):]
    answer['summary']=summary(snap)+' '+head if body.get('intent',{}).get('name')=='REMEDIATION' else summary(snap)
    if ex.get('remediation_plans') is not None:answer['remediation_plans']=ex['remediation_plans']
    from ...presentation.projection import enabled
    if not enabled():answer['attribution_v2']=snap
    answer['attribution_display_version']=1
    answer['details_kind']='table'
    from .confidence import questions
    answer['followup_questions']=questions(snap)
    body['report']=answer['summary']+'\n\n'+'\n'.join(r['label']+'：'+r['value'] for r in answer['details'])


def _additional_rows(details):
    # V2 already owns diagnosis/analysis; retain plans and unrelated custom details.
    prefixes=('诊断 · ','候选 · ','能力缺口 · ','待查队列 · ','本轮 SDK 核查 · ','留存 SDK 核查 · ')
    labels={'诊断来源','诊断停止原因','预算内待查问题','排查分析'}
    return [r for r in details if r['label'] not in labels and
            not r['label'].startswith(prefixes) and not re.match(r'^问题 \d+ · ',r['label'])]


def plan_context(snapshot):
    if not snapshot:return ''
    from .confidence import assess_candidate
    return '\n## 已校验的分层诊断（具体原因未确认时只能给条件式建议）\n'+json.dumps(
        {'problems':[{'problem_id':p['problem_id'],'service':p['service'],'object_name':p['object_name'],
        'cause':p['cause'],'candidates':[{'statement':c['statement'],'status':c['status'],
                'confidence':assess_candidate(c).model_dump(mode='json'),
                'missing':[r['question'] for r in c['requirements'] if r['state']!='satisfied']}
                for c in p['candidates']]} for p in snapshot.get('problems',[])]},ensure_ascii=False)


def markdown(body):
    snap=(body.get('explanation') or {}).get('attribution_v2')
    if not snap:return ''
    answer=(body.get('explanation') or {}).get('answer')
    render=rows
    if answer is not None and not answer.get('attribution_display_version'):
        from .legacy_display import rows as render
    return '\n## 分问题归因快照\n\n'+'\n\n'.join('### '+r['label']+'\n\n'+r['value'] for r in render(snap))+'\n'
