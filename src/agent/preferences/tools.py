"""Read tool returns an auditable receipt; private capsule enters only model context."""
from ..tools.definition import define_tool
from ..tools.sources import RawOutcome
from .read import read_for,receipt


def register(service):
    executor=getattr(service.orchestrator,'executor',None)
    if not executor:return
    if executor.registry.get('preference_read'):return
    @define_tool(name='preference_read',description='读取当前已验证用户最近成功发布的表达偏好与适用经验，不调用模型、不等待重建。主体/系统由执行器绑定，不能指定他人。返回版本与应用回执，受保护参考由上下文装配器读取；不能改变证据或授权。',
        parameters={'type':'object','properties':{},'additionalProperties':False},capabilities=('preference_read',),
        granted_roles=('preference_agent',),timeout_sec=2,rate_limit='120/min')
    async def read(context,params):
        from ..llm.context import CURRENT
        ctx=CURRENT.get()
        data=await read_for(getattr(ctx,'preference_service',None) or service,ctx) if ctx else {'version':0,'status':'no_context'}
        if ctx:ctx.preference_data=data
        safe=receipt(data)
        return RawOutcome(data=safe,raw_ref='preferences:'+str(safe.get('hash','none')),note='偏好参考不参与事实确认')
    executor.registry.register(read)


async def load(service,ctx):
    ctx.preference_service=service
    from ..execution.runtime import CURRENT as EXECUTION
    execution=EXECUTION.get()
    if execution and execution.body.get('preference_snapshot') is not None:
        return await read_for(service,ctx)
    executor=getattr(service.orchestrator,'executor',None)
    if executor and executor.registry.get('preference_read'):
        from ..common.tool_budget import CURRENT as TOOL_BUDGET,tool_budget_scope
        from contextlib import nullcontext
        state=execution.state if execution else None
        budget=state.budget if state is not None else None
        if budget and (budget.tool_calls>=budget.max_tool_calls or budget.elapsed_sec>=budget.max_wall_time_sec):
            return {'version':0,'status':'tool_budget_skipped','preferences':[],'experiences':[]}
        guard=tool_budget_scope(budget) if budget and TOOL_BUDGET.get() is None else nullcontext()
        with guard:result=await executor.call('preference_agent','preference_read',{})
        if ctx.preference_data is not None:return ctx.preference_data
        return {'version':0,'status':'tool_unavailable','preferences':[],'experiences':[]}
    return await read_for(service,ctx)
