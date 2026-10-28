"""Retain existing bounded dialogue interpretation and verification as adapters."""
import asyncio,time
from ..dialogue.contracts import metadata,snapshot
from ..dialogue.routing import quick,classify
from ..dialogue.state import bind
from ..dialogue.limits import budget_for
from ..dialogue.flow import perform,clarification
from ..dialogue.pending import ask_target
from ..common.steps import emit_step


async def propose(service,request,rows):
    hint=quick(request.message)
    if hint:return hint
    strict=max(rows,key=lambda b:metadata(b).get('task_used',{}).get('tokens',0)) if rows else None
    return await classify(service,request,rows,budget_for(service,request,strict))


def resolve(rows,request,decision,names):
    # Legacy pending references are normalized by the controller to task revisions.
    pending=next((metadata(b).get('pending_target') for b in rows if metadata(b).get('pending_target')),None)
    if decision.action=='clarify_target' and pending and pending.get('action'):
        decision.action=pending['action'];decision.reason='补充调查目标后接续原对话动作'
    return bind(rows,request,decision,names)


async def execute(service,request,trace_id,usage,on_event,decision,source,p,rows,reason,control):
    budget=budget_for(service,request,source)
    if reason:return ask_target(request,trace_id,reason,budget,rows,decision)
    if source is None:return clarification(request,trace_id,'没有可恢复的诊断，请先指定目标或开始排查。',budget)
    budget.tokens=sum(r.input_tokens+r.output_tokens for r in usage)
    budget.llm_calls=sum(max(1,getattr(r,'attempts',1)) for r in usage)
    emit_step(on_event,'dialogue','识别任务内对话动作',decision.model_dump(),result=decision.action)
    try:
        body=await asyncio.wait_for(perform(service,request,source,p,decision,budget,trace_id,usage,on_event,task_control=control),timeout=max(.05,budget.max_wall_time_sec))
    except Exception as exc:
        from ..api.constants import logger
        from ..tools.availability import safe_detail
        logger.error('任务推进失败 trace_id=%s action=%s type=%s reason=%s',trace_id,decision.action,type(exc).__name__,safe_detail(str(exc)))
        import traceback
        logger.error('任务推进失败位置 %s',[(r.name,r.lineno) for r in traceback.extract_tb(exc.__traceback__)])
        from ..dialogue.view import response
        body=response(request,trace_id,source,p,decision,'本次推进未完成：'+type(exc).__name__+'；保留原判断和资料。',[],budget,reason='请核对来源或选择其他核查方向。')
    body['cost'].update(tool_calls=budget.tool_calls,input_tokens=sum(r.input_tokens for r in usage),output_tokens=sum(r.output_tokens for r in usage))
    body['explanation']['model_calls']=[{k:getattr(r,k,None) for k in ('role','model','attempts','input_tokens','output_tokens','latency_ms','ok')} for r in usage]
    if service.state_note:body['degraded'].append(service.state_note)
    emit_step(on_event,'answer','回答任务推进问题',{'action':decision.action},result=body['explanation']['answer']['summary'])
    return body
