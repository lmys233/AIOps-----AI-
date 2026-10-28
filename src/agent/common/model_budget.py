"""物理请求前预留、响应后结算；估算预留不计入已报告用量。"""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import time
from .tokens import estimate_tokens

_guard=ContextVar('model_attempt_budget',default=None)


def guard_attempt(body):
    guard=_guard.get()
    return guard.begin(body) if guard else None


def settle_attempt(ticket,reported,known):
    if ticket:
        budget,amount=ticket
        budget.tokens+=reported
        budget.reserved_tokens=max(0,budget.reserved_tokens-(amount if known else min(amount,reported)))


def refund_placeholder():
    guard=_guard.get()
    if guard and guard.pre_reserved and not guard.attempts:
        guard.budget.llm_calls=max(0,guard.budget.llm_calls-1)
        guard.pre_reserved=False


def active():return _guard.get() is not None


class Guard:
    def __init__(self,budget,pre_reserved):
        self.budget=budget;self.pre_reserved=pre_reserved;self.attempts=0
        self.started,self.base=time.monotonic(),budget.elapsed_sec

    def begin(self,body):
        from ..llm.gateway import LLMError
        from .enums import ErrorCode
        budget=self.budget
        budget.observe_elapsed(self.base+time.monotonic()-self.started)
        size=estimate_tokens(json.dumps({k:v for k,v in body.items() if k!='max_tokens'},ensure_ascii=False))
        available=budget.max_tokens-budget.tokens-budget.reserved_tokens-size
        reserved_first=self.pre_reserved and not self.attempts and budget.llm_calls>0
        calls=budget.llm_calls-int(reserved_first)
        if calls>=budget.max_llm_calls or available<1 or budget.aborted or budget.elapsed_sec>=budget.max_wall_time_sec:
            raise LLMError(ErrorCode.INTERNAL_ERROR,'模型/Token/耗时预算不足，停止供应商请求',failure_kind='task_budget')
        body['max_tokens']=min(int(body['max_tokens']),int(available))
        amount=size+body['max_tokens'];budget.reserved_tokens+=amount
        if not reserved_first:budget.spend_llm(calls=1)
        self.attempts+=1
        return budget,amount


@contextmanager
def model_budget_scope(budget,*,pre_reserved=True):
    token=_guard.set(Guard(budget,pre_reserved))
    try:yield
    finally:_guard.reset(token)
