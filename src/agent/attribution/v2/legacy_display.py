"""Frozen rendering for Markdown belonging to incidents saved before display grouping."""


def rows(snapshot):
    from .presentation import STATE, CHECK
    out=[]
    if snapshot.get('source',{}).get('kind')=='previous_snapshot':
        out.append({'label':'诊断来源','value':'上一轮事故 '+snapshot['source']['incident_id']+'；未重新核查当前状态'})
    for p in snapshot.get('problems',[]):
        title=p['service']+' · '+p['error_type']+((' · '+p['object_name']) if p['object_name'] else '')
        pieces=['问题 '+p['problem_id'], '已观测事实：'+p['fact']['statement'],
            '故障机制：'+(p['mechanism']['statement'] or '尚未确认'),
            '形成原因：'+(p['cause']['statement'] if p['cause']['status']=='confirmed' else '待核查'),
            '证据：'+ '、'.join(p['evidence_ids']),
            '口径：'+p.get('current_state_note','历史窗口采样，不能断定此刻仍发生')]
        if p.get('request_id'):pieces.append('请求：'+p['request_id'])
        out.append({'label':'诊断 · '+title,'value':'；'.join(pieces)})
        for c in p['candidates']:
            checks='；'.join(r['question']+'：'+CHECK[r['state']]+
                ('（'+r['reason']+'）' if r['reason'] else '') for r in c['requirements'])
            refs='、'.join(c['supporting']+c['contradicting']) or '尚无形成原因的独立支持'
            out.append({'label':'候选 · '+title+' · '+c['statement'],
                'value':c['candidate_id']+'；'+STATE[c['status']]+'；'+checks+'；引用：'+refs})
    labels={'unconfigured':'来源未配置','unsupported':'适配器未接入','invalid_source':'来源不可验证'}
    for gap in snapshot.get('capability_gaps',[]):
        out.append({'label':'能力缺口 · '+gap['service']+' · '+gap['tool'],
            'value':labels.get(gap['status'],gap['status'])+'（未调用）；'+gap['reason']+'；接入方式：'+gap['remedy']})
    out.append({'label':'诊断停止原因','value':snapshot.get('stop_reason','')})
    if snapshot.get('pending_problems'):
        out.append({'label':'预算内待查问题','value':'、'.join(snapshot['pending_problems'])})
    return out
