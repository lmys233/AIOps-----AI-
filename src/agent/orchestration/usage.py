"""将请求内持续追加的模型账本同步到预算，不重复计账。"""


def sync_usage(state) -> None:
    records=state.flags.get('usage') or ()
    from ..llm.accounting import summary
    totals=summary(records);tokens=totals['reported_tokens'];calls=totals['physical_attempts']
    state.budget.tokens=max(state.budget.tokens,tokens)
    state.budget.llm_calls=max(state.budget.llm_calls,calls)
    state.budget.reserved_tokens=max(state.budget.reserved_tokens,totals['reserved_tokens'])
    from ..execution.runtime import CURRENT
    execution=CURRENT.get()
    if execution is not None:
        used=execution.body['used']
        state.budget.tokens=max(state.budget.tokens,used['tokens'])
        state.budget.llm_calls=max(state.budget.llm_calls,used['llm_calls'])
        state.budget.tool_calls=max(state.budget.tool_calls,used['tool_calls'])
        state.budget.reserved_tokens=max(state.budget.reserved_tokens,execution.body.get('reserved_model_tokens',0))
