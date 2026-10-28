"""One deadline and finite physical attempts, including assembly, backoff and fallbacks."""
import asyncio,json,time
from contextvars import ContextVar
from dataclasses import dataclass,asdict
from functools import wraps
from email.utils import parsedate_to_datetime
from datetime import datetime,timezone
from .gateway import LLMError
from ..common.enums import ErrorCode

CURRENT=ContextVar('model_call_policy',default=None)


class ModelFailure(LLMError):
    def __init__(self,kind,message,attempts=0):
        self.failure_kind=kind
        super().__init__(ErrorCode.LLM_TIMEOUT if kind=='stage_deadline' else ErrorCode.INTERNAL_ERROR,
                         message,attempts=attempts)


@dataclass(frozen=True)
class Policy:
    timeout:float=60
    max_attempts:int=4
    backoff:float=.2
    max_backoff:float=2

    def __post_init__(self):
        if not 0<self.timeout<=120 or isinstance(self.max_attempts,bool) or not isinstance(self.max_attempts,int) or not 1<=self.max_attempts<=6:
            raise ValueError('Invalid finite model deadline/attempts')
        if not 0<=self.backoff<=self.max_backoff<=5:raise ValueError('Invalid bounded retry backoff')


@dataclass
class Invocation:
    policy:Policy
    deadline:float
    timeout_scope:object=None

    def remaining(self):return max(0,self.deadline-time.monotonic())


def bounded(method):
    @wraps(method)
    async def wrapped(gateway,*args,**kwargs):
        from .accounting import collect_records
        policy=gateway.policy_for(kwargs['role'])
        scope=asyncio.timeout(policy.timeout)
        context=Invocation(policy,time.monotonic()+policy.timeout,scope);token=CURRENT.set(context)
        try:
            with collect_records() as records:
                try:
                    async with scope:return await method(gateway,*args,**kwargs)
                except TimeoutError as error:
                    raise ModelFailure('stage_deadline','模型阶段总期限已到；保留尝试及未知费用，不重跑已完成阶段',
                        attempts=sum(r.attempts for r in records)) from error
        finally:CURRENT.reset(token)
    return wrapped


async def retry(meter,row,kind,response=None):
    context=CURRENT.get()
    if context is None:raise RuntimeError('Model retry requires invocation policy')
    policy=context.policy
    if len(meter.rows)>=policy.max_attempts:
        raise ModelFailure('attempt_limit','本逻辑调用物理尝试已达到上限',len(meter.rows))
    delay=0
    if kind in ('http_transient','transport_timeout'):
        delay=min(policy.max_backoff,policy.backoff*2**(len(meter.rows)-1))
        header=response.headers.get('Retry-After') if response is not None else None
        if header:
            try:requested=float(header)
            except ValueError:
                try:requested=max(0,(parsedate_to_datetime(header)-datetime.now(timezone.utc)).total_seconds())
                except (ValueError,TypeError,OverflowError):requested=0
            # Do not retry early if the server requests a wait larger than our cap.
            if requested>policy.max_backoff:
                raise ModelFailure('retry_after_exceeds_policy','上游重试等待超出本阶段允许范围',len(meter.rows))
            delay=max(delay,max(0,requested))
    if context.remaining()<=delay+.01:
        raise ModelFailure('stage_deadline','剩余期限不足以等待及重试',len(meter.rows))
    row.update(retry_reason=kind,retry_backoff_sec=delay)
    from ..execution.runtime import model_attempt_event
    await model_attempt_event(row)
    if delay:await asyncio.sleep(delay)
