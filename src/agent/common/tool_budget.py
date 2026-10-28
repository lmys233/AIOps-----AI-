"""Per-request tool attempt reservations, shared across concurrent agents."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

CURRENT=ContextVar('aiops_tool_attempt_budget',default=None)


@contextmanager
def tool_budget_scope(budget,pre_reserved=False):
    token=CURRENT.set((budget,pre_reserved,time.monotonic(),budget.elapsed_sec))
    try:yield
    finally:CURRENT.reset(token)


def reserve_attempt(first):
    scope=CURRENT.get()
    if scope is None:return None,None
    budget,pre_reserved,started,base=scope
    budget.observe_elapsed(base+time.monotonic()-started)
    left=budget.max_wall_time_sec-budget.elapsed_sec
    if left<=0:return '请求耗时预算已耗尽',0
    if not (first and pre_reserved):
        if budget.tool_calls>=budget.max_tool_calls:return '工具调用预算已耗尽',left
        budget.spend_tool()
    return None,left
