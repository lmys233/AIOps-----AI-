"""One status label for live, history and export."""
def label(body):
    ex=body.get('explanation') or {};d=ex.get('dialogue') or {}
    if ex.get('command_chain') and not ex.get('attribution_v2'):
        return {'failed':'告警查询未完成','clarify':'需要明确告警筛选条件'}.get(body.get('status'),'已查询告警')
    active=ex.get('active_probe') or {}
    if active.get('action')=='ask':return '等待接口信息或探测确认'
    if active.get('runs') and not (ex.get('attribution_v2') or {}).get('problems'):
        return '已完成主动核验' if any(r['result'].get('ok') for r in active['runs']) else '主动核验未完成'
    if (body.get('intent') or {}).get('name')=='KNOWLEDGE_QA' and body.get('status')!='failed':
        if body.get('status')=='answered':
            quality=((ex.get('answer') or {}).get('knowledge') or {}).get('quality') or {}
            if quality:return '已校验知识参考' if quality.get('status') in ('passed','repaired') else '已提供摘录，答案未校验'
            return '已提供知识参考'
        return {'knowledge_no_match':'没有匹配知识','knowledge_needs_reference':'需要明确指代'}.get((ex.get('answer') or {}).get('source'),'知识查询未完成')
    actions={'explain_result':'已说明判断依据','explain_gaps':'已说明证据缺口',
        'submit_evidence':'已接收待核验资料','compare_candidates':'已比较候选原因'}
    if d.get('completed') and d.get('action') in actions:return actions[d['action']]
    advisor=(ex.get('advisor_outcome') or {}).get('status') or (ex.get('answer') or {}).get('advisor_status')
    if advisor=='plans_ready':return '已提供修复方案'
    if advisor=='needs_evidence':return '需要补充证据'
    ps=(ex.get('attribution_v2') or {}).get('problems') or []
    if ps and body.get('status')!='failed':
        return '问题原因已确认' if all(p['cause']['status']=='confirmed' for p in ps) else '已发现问题，原因待核查'
    return {'concluded':'已定位根因','clarify':'需要补充信息','abstained':'未得出结论',
        'budget_exhausted':'预算用尽','rejected':'已拒绝','failed':'系统错误'}.get(body.get('status'),'已结束')
