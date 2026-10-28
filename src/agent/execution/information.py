"""Explain saved progress without restarting the graph or spending tool/model budget."""
from ..adapters.task_runtime import controller,executions
from ..adapters.task_output import refusal
from ..common.steps import emit_step

async def explain(service,request,task,trace_id,on_event):
    repo=controller(service).repository;store=getattr(repo,'executions',None) or executions(service)
    run=await store.read(task.scope,task.execution_id)
    if not run:raise ValueError('执行台账不可读，不能根据聊天摘要补造进度')
    memory=run.get('task_memory') or task.memory
    facts=memory.get('known_facts') or [];gaps=memory.get('open_questions') or memory.get('gaps') or []
    summary=f"任务尚未完成，保存的下一节点是 {run.get('node') or '入口'}；已有 {len(facts)} 条事实引用。"
    summary+=('上一轮仍有有效执行租约，请稍后查看。' if run.get('busy') else
        '可回复“继续任务”恢复执行；是否允许恢复仍需核对调用状态、权限与剩余预算。')
    body=refusal(request,trace_id,ValueError(summary))
    body['explanation']['task_memory']=memory
    rows=[{'label':'任务目标','value':memory.get('goal') or task.original},
        {'label':'原查询截止','value':memory.get('query_anchor') or run.get('started_at') or '尚未确定'},
        {'label':'已保存事实','value':'；'.join(f"{f['evidence_id']}：{f['summary']}" for f in facts[:10]) or '尚无已保存事实'},
        {'label':'已记录缺口','value':'；'.join(str(g) for g in gaps[:10]) or '目前是执行中断，尚未完成证据缺口裁决；不猜测用户必须提供什么资料'}]
    body['explanation']['answer']['details']=rows
    emit_step(on_event,'execution_memory','查看任务记忆',{'task_id':task.task_id,'execution_id':task.execution_id},result=summary)
    return body
