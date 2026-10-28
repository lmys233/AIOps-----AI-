"""Shared diagnostic phase checks; an already entered round may finish."""


def budget_reason(budget):
    if budget.aborted: return '人工中止'
    for used, limit, label in [(budget.elapsed_sec, budget.max_wall_time_sec, '耗时预算'),
        (budget.tool_calls, budget.max_tool_calls, '工具预算'),
        (budget.llm_calls, budget.max_llm_calls, '模型预算'),
        (budget.tokens+budget.reserved_tokens, budget.max_tokens, 'token预算')]:
        if used >= limit: return label + '耗尽'
    return ''
