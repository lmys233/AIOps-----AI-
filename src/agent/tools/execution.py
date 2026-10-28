"""Execution/retry/result conversion after policy gates; every invocation shares the audit hook."""
import asyncio
from .errors import SourceUnavailable,QueryRejected,ToolRefused
from ..common.contracts import ToolResult
from ..common.tool_budget import reserve_attempt
from ..observed.attempts import invoke


async def run(executor,definition,params,started):
    from ..execution.runtime import CURRENT
    execution=CURRENT.get()
    if execution is not None:
        return await execution.tool(definition,params,lambda:run_untracked(executor,definition,params,started))
    return await run_untracked(executor,definition,params,started)


async def run_untracked(executor,definition,params,started):
    spec=definition.spec;ref=executor._raw_ref_hint(spec.name,params)
    attempts=0
    while True:
        attempts+=1;stopped,remaining=reserve_attempt(attempts==1)
        if stopped:return failure('BUDGET_EXHAUSTED','BudgetStop',stopped,ref,executor,started)
        timeout=min(spec.timeout_sec,remaining) if remaining is not None else spec.timeout_sec
        from ..execution.runtime import CURRENT
        execution=CURRENT.get()
        if execution:
            stopped=await execution.attempt(timeout)
            if stopped:return failure('TOOL_UNAVAILABLE','RecoveryBlocked',stopped,ref,executor,started)
        try:
            raw=await invoke(definition,executor.context,params,timeout)
        except Exception as exc:
            if execution:await execution.finish_attempt(type(exc).__name__)
            if isinstance(exc,asyncio.TimeoutError) and timeout<spec.timeout_sec:
                return failure('BUDGET_EXHAUSTED','BudgetStop','请求耗时预算截止，不能当作 SDK 自身超时',ref,executor,started)
            retry=(isinstance(exc,asyncio.TimeoutError) or getattr(exc,'retryable',False)) and spec.idempotent and attempts<2
            if retry:
                if not isinstance(exc,asyncio.TimeoutError):await asyncio.sleep(getattr(exc,'retry_after',.2))
                continue
            return exception_result(exc,spec,attempts,ref,executor,started)
        except BaseException:
            if execution:await execution.finish_attempt('interrupted')
            raise
        if execution:await execution.finish_attempt()
        return summarize(raw,definition,ref,executor,started)


def failure(code,kind,text,ref,executor,started,degraded=True):
    return ToolResult(ok=False,degraded=degraded,error_code=code,error_type=kind,
        summary=text,raw_ref=ref,elapsed_ms=(executor._clock()-started)*1000)


def exception_result(exc,spec,attempts,ref,executor,started):
    if isinstance(exc,asyncio.TimeoutError):
        why='已重试 1 次仍超时' if attempts>1 else '超时（非幂等工具不重试）' if not spec.idempotent else '超时'
        return failure('TOOL_TIMEOUT','TimeoutError',f'{spec.name} {why}（>{spec.timeout_sec}s，实际尝试 {attempts} 次）—— 建议缩小时间窗或加 label 过滤后重试',ref,executor,started)
    if isinstance(exc,SourceUnavailable):
        return ToolResult.unavailable(error_type=exc.error_type,summary=f'{spec.name} 没查成：{exc.message}（工具尝试 {attempts} 次；这一路缺失，不等于没有异常）',
            raw_ref=ref,elapsed_ms=(executor._clock()-started)*1000)
    if isinstance(exc,QueryRejected):return failure('TOOL_INVALID_PARAMS','QueryRejected',f'{spec.name} 的查询被数据源拒绝：{exc}',ref,executor,started,False)
    if isinstance(exc,ToolRefused):return failure(exc.error_code,exc.error_type,exc.message,ref,executor,started)
    import logging
    logging.getLogger(__name__).exception('Tool execution failed: %s',spec.name)
    return failure('INTERNAL_ERROR',type(exc).__name__,f'{spec.name} 内部错误（{type(exc).__name__}）—— 已记日志；这一路信号缺失',ref,executor,started)


def summarize(raw,definition,ref,executor,started):
    try:data=definition.summarize(raw.data)
    except Exception as exc:
        return failure('INTERNAL_ERROR','SummarizeFailed:'+type(exc).__name__,f'{definition.name} 的结果无法摘要化（{type(exc).__name__}）',raw.raw_ref or ref,executor,started)
    text=str(data.get('summary') or '')
    if raw.note and raw.note not in text:text=f'{text}（{raw.note}）' if text else raw.note
    return ToolResult(ok=True,data=data,degraded=False,summary=text[:400],raw_ref=raw.raw_ref or ref,
        elapsed_ms=(executor._clock()-started)*1000)
