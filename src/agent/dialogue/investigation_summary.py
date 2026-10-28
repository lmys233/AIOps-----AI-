"""Describe investigation scope from the same leads actually rendered in the table."""

LEAD_PREFIX = '关联核查线索 · '


def summarize(p, items, related, fallback):
    target = p['service'] + (' 的 ' + p['endpoint'] if p.get('endpoint') else '')
    head = '围绕' + target + '异常（' + p['error_type'] + '，' + p['problem_id'] + '），'
    if p['cause']['status'] == 'confirmed':
        head += '已确认形成原因：' + p['cause']['statement'] + '。其他未验证方向不能推翻该结论，也不证明全量恢复。'
    else:
        head += '优先核查直接触发来源；这是针对你提出的现象确定排查范围，尚未确认形成原因。'
    leads = [r['label'][len(LEAD_PREFIX):] for r in related if r['label'].startswith(LEAD_PREFIX)]
    if leads:
        head += '同时，' + '；'.join(leads) + '作为待验证的关联线索，尚不能认定它们导致了目标异常。'
    if items:
        head += ('下表列出你指定候选的资料需求，以及已展示关联线索所需的传递路径记录。' if p.get('_focused_candidate') and leads else
                 '下表列出你指定候选需要补充的数据。' if p.get('_focused_candidate') else
                 '下表按直接原因候选' + ('及关联线索' if leads else '') + '列出需要补充的数据，帮助我们逐项确认或排除。')
        if p['cause']['status'] != 'confirmed':head += '当前证据不足以按原因概率排序。'
    else:
        head += ('本轮无需重补已证实条件；其他未列出方向仍未排除。' if p['cause']['status'] == 'confirmed' else fallback)
        if leads:head += '关联线索尚需下表中的路径记录，不能因直接候选无需补证就认为关联已确认。'
    if any(r['label'] == '关联覆盖范围' for r in related):
        head += '关联线索本次仅展示前 12 类，其余尚未检查。'
    return head
