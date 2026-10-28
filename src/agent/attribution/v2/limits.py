"""增强模型不能越过请求内已有更严格的预算。"""
import asyncio
import time
from ...common.tokens import estimate_tokens
from ...orchestration.usage import sync_usage
from .engine import budget_reason


def model_limits(state,prompt,schema,wanted):
    sync_usage(state)
    remaining=state.budget.max_tokens-state.budget.tokens-state.budget.reserved_tokens
    size=estimate_tokens(prompt)+estimate_tokens(str(schema.model_json_schema()))
    if budget_reason(state.budget) or remaining-size<1024:
        state.add_note('方案模型预算不足：沿已有证据保留条件式核查建议')
        return None
    state.budget.spend_llm(calls=1)
    return {'max_tokens':min(wanted,remaining-size)}


async def bounded_generate(gateway,budget,*args,**kwargs):
    started,base=time.monotonic(),budget.elapsed_sec
    pending=asyncio.create_task(gateway.generate(*args,**kwargs))
    try:
        return await asyncio.wait_for(pending,
            timeout=max(.001,budget.max_wall_time_sec-base))
    except TimeoutError:
        if pending.cancelled():budget.observe_elapsed(budget.max_wall_time_sec)
        raise
    finally:budget.observe_elapsed(base+time.monotonic()-started)
