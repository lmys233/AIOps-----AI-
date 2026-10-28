import hashlib,json,time
from datetime import datetime,timezone
from uuid import uuid4
from ..observed.context import current
from ..common.tool_budget import tool_budget_scope
from .builtins import bind
from .planning import plan,event
from .parsing import parse


async def run(engine,text,budget,*,emit=None,timezone_name='Asia/Shanghai',preview=False):
    from ..execution.runtime import CURRENT
    execution=CURRENT.get();scope=current();chain=parse(text)
    anchor=execution.body['started_at'] if execution else datetime.now(timezone.utc).isoformat()
    if execution and 'command_snapshot' in execution.body:snapshot=execution.body['command_snapshot']
    else:
        snapshot=await engine.warnings.snapshot(scope)
        if execution:execution.body['command_snapshot']=snapshot;await execution.save()
    upstream=None;steps=[];last=None;started=time.monotonic();base=budget.elapsed_sec
    for index,step in enumerate(chain):
        budget.observe_elapsed(base+time.monotonic()-started)
        if budget.exhausted():raise ValueError(budget.exhausted())
        try:decision=await plan(engine,chain,index,upstream,budget,scope,anchor,timezone_name,emit,preview=preview)
        except Exception as exc:return dict(chain=chain,steps=steps,result=upstream,action='failed',question='命令规划未完成：'+type(exc).__name__+'；已停止后续步骤，没有猜测筛选结果。')
        last=decision
        budget.observe_elapsed(base+time.monotonic()-started)
        if budget.exhausted():return dict(chain=chain,steps=steps,result=upstream,action='failed',question=budget.exhausted())
        if decision.action=='clarify':return dict(chain=chain,steps=steps,result=upstream,action='clarify',question=decision.question or decision.reason)
        selection=upstream['selection'] if upstream else None
        ref=hashlib.sha256(json.dumps([scope.tenant,scope.observed_id,scope.env,scope.cluster,index,selection,snapshot],sort_keys=True).encode()).hexdigest()
        params=dict(decision.arguments,input_ref=ref)
        payload=dict(call_id='command-tool-'+uuid4().hex,name=decision.tool,title=engine.registry.entries[step['command']].usage,
            role='command_agent',params=params,why=decision.reason)
        event(emit,dict(payload,phase='start'));tick=time.monotonic()
        try:
            with bind(engine,selection,snapshot),tool_budget_scope(budget):
                result=await engine.registry.use(step['command'],params)
        except BaseException as exc:
            event(emit,dict(payload,phase='done',ok=False,error_code='COMMAND_EXECUTION_STOPPED',
                result=dict(error=type(exc).__name__),elapsed_ms=(time.monotonic()-tick)*1000))
            if not isinstance(exc,Exception):raise
            return dict(chain=chain,steps=steps,result=upstream,action='failed',question='命令执行未完成：'+type(exc).__name__+'；未继续后续步骤。')
        event(emit,dict(payload,phase='done',ok=result.ok,degraded=result.degraded,error_code=result.error_code or '',
            result=result.model_dump(mode='json'),elapsed_ms=(time.monotonic()-tick)*1000))
        budget.observe_elapsed(base+time.monotonic()-started)
        steps.append(dict(index=index,command=step,decision=decision.model_dump(mode='json'),params=params,result=result.model_dump(mode='json')))
        if not result.ok:return dict(chain=chain,steps=steps,result=upstream,action='failed',question=result.summary)
        upstream=result.data['records'][0]
    return dict(chain=chain,steps=steps,result=upstream,action=last.action,question='')
