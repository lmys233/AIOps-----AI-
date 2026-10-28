"""Group related diagnoses for display; verification remains scoped to each problem."""
from collections import OrderedDict


def unique(values):
    return list(dict.fromkeys(values))


def _base(p):
    # Unknown query failures must not be merged solely because their error labels match.
    symptom = ''
    if not p['mechanism']['statement'] and not p.get('object_name'):
        symptom = (p['exception_type'], p['fact']['statement'])
    return (p['service'], p.get('env', ''), p.get('cluster', ''), p['error_type'],
            p.get('object_name', ''), symptom)


def groups(snapshot):
    buckets = OrderedDict()
    for p in snapshot.get('problems', []):
        buckets.setdefault(_base(p), []).append(p)
    result = []
    for members in buckets.values():
        # Missing route/fingerprint can join only one compatible display scope.
        # This is not a claim that the missing field was observed on those records.
        shapes = unique((p.get('endpoint', ''), p.get('query_fingerprint', '')) for p in members)
        resolved = OrderedDict()
        for p in members:
            shape = (p.get('endpoint', ''), p.get('query_fingerprint', ''))
            matches = [s for s in shapes if s != shape and all(
                not a or a == b for a, b in zip(shape, s))]
            if len(matches) == 1:
                shape = matches[0]
            resolved.setdefault(shape, []).append(p)
        result.extend(resolved.values())
    return result


def title(members):
    p = members[0]
    parts = [p['service'], ('指标变化 '+p['metric_name']) if p.get('metric_name') else p['error_type']]
    for field in ('object_name', 'endpoint', 'query_fingerprint'):
        parts.extend(unique(x[field] for x in members if x.get(field)))
    for field, label in (('env', '环境'), ('cluster', '集群')):
        if p.get(field):
            parts.append(label + '=' + p[field])
    return ' · '.join(parts)


def scope(p):
    fields = [p['problem_id']]
    for key, label in (('env', '环境'), ('cluster', '集群'), ('instance', '实例'),
                       ('boot_id', '启动'), ('endpoint', '接口'), ('request_id', '请求')):
        if p.get(key):
            fields.append(label + '=' + p[key])
    return ' / '.join(fields)


def diagnosis(members, *, previous_snapshot=False):
    p = members[0]
    facts = unique(x['fact']['statement'] for x in members)
    mechanisms = unique(x['mechanism']['statement'] or '尚未确认' for x in members)
    confirmed = [x for x in members if x['cause']['status'] == 'confirmed']
    causes = unique(x['cause']['statement'] for x in confirmed)
    cause = '待核查'
    if len(confirmed) == len(members) and len(causes) == 1:
        cause = causes[0]
    elif confirmed:
        cause = '部分观测已确认，其他仍待核查；' + '；'.join(
            x['problem_id'] + '：' + x['cause']['statement'] for x in confirmed)
    pieces = ['问题 ' + '、'.join(x['problem_id'] for x in members),
              ('样本事实：' if len(facts) > 1 else '已观测事实：') + facts[0],
              '故障机制：' + '；'.join(mechanisms), '形成原因：' + cause,
              '证据：' + '、'.join(unique(e for x in members for e in x['evidence_ids'])),
              '口径：' + '；'.join(unique((x.get('current_state_note',
                  '历史窗口采样，不能断定此刻仍发生').replace('本轮 ', '留存核查 ')
                  if previous_snapshot else x.get('current_state_note',
                  '历史窗口采样，不能断定此刻仍发生')) for x in members))]
    if len(members) > 1:
        pieces.append(f'归并展示 {len(members)} 个观测分组，逐组核查，不代表同一次故障')
    pieces.append('观测范围：' + '；'.join(scope(x) for x in members))
    return {'label': '诊断 · ' + title(members), 'value': '；'.join(pieces)}


def candidates(members, state, checks):
    grouped = OrderedDict()
    for p in members:
        for c in p['candidates']:
            key = (c['mechanism_key'], c['statement'])
            grouped.setdefault(key, []).append((p, c))
    out = []
    for entries in grouped.values():
        first = entries[0][1]
        requirements = OrderedDict()
        for p, c in entries:
            for r in c['requirements']:
                key = (r['question'], r['state'], r['reason'])
                requirements.setdefault(key, []).append(p['problem_id'])
        lines = []
        for (question, status, reason), ids in requirements.items():
            line = question + '：' + checks[status] + ('（' + reason + '）' if reason else '')
            if len(entries) > 1:
                line += '；适用 ' + '、'.join(unique(ids))
            lines.append(line)
        refs = unique(e for _, c in entries for e in c['supporting'] + c['contradicting'])
        ids = '、'.join(c['candidate_id'] + ('（' + state[c['status']] + '）'
            if len(entries) > 1 else '') for _, c in entries)
        statuses = unique(state[c['status']] for _, c in entries)
        outcome = statuses[0] if len(statuses) == 1 else '各观测结论分别保留'
        from .confidence import assess_candidate,describe
        confidence=unique(describe(assess_candidate(c)) for _,c in entries if 'confidence' in c)
        out.append({'label': '候选 · ' + title(members) + ' · ' + first['statement'],
            'value': ids + '；' + outcome + ('；' + '；'.join(confidence) if confidence else '') + '；' + '；'.join(lines)
                + '；引用：' + ('、'.join(refs) or '尚无形成原因的独立支持')})
    return out
