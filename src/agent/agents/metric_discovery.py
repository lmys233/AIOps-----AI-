"""Discover metric metadata through the same authorized, budgeted tool executor."""
from dataclasses import replace
from .base import AgentOutcome,ToolRequest
from .batches import contexts
from .query_labels import service_pattern
from ..tools.metric_catalog import checked

MAX_PAGES=4

async def prepare(runner,ctx,agents,on_event=None):
    source=getattr(getattr(runner.executor,'context',None),'prometheus',None)
    definition=runner.executor.spec_of('promql_read')
    agent=next((a for a in agents if a.role=='metrics_agent'),None)
    if agent is None or not callable(getattr(source,'catalogue',None)) or not definition or 'operation' not in definition.spec.input_schema.get('properties',{}):
        return ctx,None
    merged=AgentOutcome(role=agent.role);descriptors=[];covered=[];sequence=0
    for batch in contexts(ctx):
        cursor=None;seen=set();batch_rows=[]
        for _ in range(MAX_PAGES):
            sequence+=1
            request=ToolRequest(tool='promql_read',params={'operation':'catalog',
                'query':'{service=~'+__import__('json').dumps(service_pattern(batch.services))+'}',
                **batch.query_window(),'limit':64,**({'cursor':cursor} if cursor else {})},
                signal='metric_catalog',purpose='aux',rationale='读取当前登记服务的实际指标目录、类型、单位与覆盖；不据此判正常',
                call_id='metrics_agent-catalog-'+str(sequence),retain_aux_result=True)
            from ..queries.binding import for_request
            request=replace(request,query_spec=for_request(batch,request,definition))
            outcome=await agent.run_call(batch,request,runner.executor,on_event=on_event)
            merged.merge(outcome)
            result=outcome.results.get('metric_catalog')
            if not result or not result.ok:
                merged.aux.append('指标目录未取得完整结果；该批保留旧指标查询，不能按缺少目录排除异常')
                break
            try:data=checked(result.data,batch.services)
            except (ValueError,PermissionError) as exc:
                merged.aux.append('指标目录未通过契约/作用域核查：'+str(exc));break
            batch_rows.extend(data['metrics']);cursor=data.get('next_cursor')
            if not cursor:
                if data['complete']:covered.extend(batch.services)
                else:merged.aux.append('指标目录覆盖不完整；未列出的指标仍未知')
                break
            if cursor in seen:
                merged.aux.append('指标目录游标重复，停止分页并保留覆盖缺口');break
            seen.add(cursor)
        else:merged.aux.append('指标目录已达到分页上限；其余指标未查')
        descriptors.extend(batch_rows)
    return replace(ctx,metric_catalog=tuple(descriptors),metric_services=tuple(dict.fromkeys(covered))),merged
