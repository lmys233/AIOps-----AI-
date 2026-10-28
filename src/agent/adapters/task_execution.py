"""Adapter: existing diagnostic graph executes one authorized task command."""
from ..adapters.task_route import bound,remaining_budget
from ..adapters import task_dialogue
from dataclasses import asdict


async def execute(service,request,task,cmd,extra,trace_id,usage,on_event,incident_id):
    command_outcome=None;command_message=None;budget=remaining_budget(service,request,task)
    from ..commands.parsing import has_commands
    if extra.get('command_chain') or cmd.action=='resume_execution' and has_commands(task.original):
        from ..commands.chat import prepare
        body,command_message,command_outcome=await prepare(service,request,task,trace_id,incident_id,on_event,budget)
        if body:return body
    if extra.get('execution_info'):
        from ..execution.information import explain
        return await explain(service,request,task,trace_id,on_event)
    if 'decision' in extra:
        if extra.get('task_wide'):
            from ..adapters.task_information import information
            from ..common.steps import emit_step
            body=information(service,request,trace_id,extra['source'],extra['decision'],remaining_budget(service,request,task))
            emit_step(on_event,'answer','说明整个任务的证据缺口',{'task_id':task.task_id},result=body['explanation']['answer']['summary'])
            return body
        return await task_dialogue.execute(service,request,trace_id,usage,on_event,
            extra['decision'],extra['source'],extra['p'],extra['rows'],extra['reason'],{'task_id':task.task_id,'action':cmd.action})
    inherited=None;active=[];source=None
    if task.diagnosis_id and cmd.action=='select_plan':
        source=await service.get_incident(task.diagnosis_id)
        inherited=dict(incident_id=task.diagnosis_id,at=(source.get('explanation') or {}).get('export_generated_at') or '',evidence=source.get('evidence',[]),
            hypotheses=source.get('hypotheses',[]),attribution_v2=source.get('explanation',{}).get('attribution_v2'),
            remediation_plans=(source.get('explanation') or {}).get('remediation_plans') or [],
            **{k:v for k,v in source.items() if k in ('slots','cost')}) if source else None
    locked=bound(task,'REMEDIATION' if cmd.action=='select_plan' else None) if cmd.action!='new_task' and task.purpose else None
    if command_message:locked=bound(task,'INCIDENT_RCA')
    if cmd.action=='select_plan':active=[{'plans':[asdict(extra['plan'])]}]
    message=task.original if cmd.action in ('fill_scope','probe_endpoint','reinvestigate','resume_execution') else request.message
    if extra.get('answer_context'):message=extra['answer_context']['query']
    if command_message:message=command_message
    control={'task_id':task.task_id,'action':cmd.action,'outcome':locked}
    if extra.get('probe_input'):control['webprobe_context']=extra['probe_input']
    if cmd.action=='resume_execution':
        from ..execution.runtime import CURRENT
        execution=CURRENT.get()
        if execution and execution.body.get('probe_context'):control['webprobe_context']=execution.body['probe_context']
    result=await service.orchestrator.run(session_id=request.session_id,trace_id=trace_id,message=message,
        env=task.scope.env,cluster=task.scope.cluster,usage=usage,options=request.options,
        incident_id=incident_id,active_tasks=active,inherited=inherited,on_event=on_event,
        deployment_form=service.deployment_form,budget=budget,task_control=control)
    body=await service._finish_new_result(result,request,trace_id,usage,[],
        getattr(service.orchestrator,'session',None),message,'',on_event)
    if command_outcome:
        body['explanation']['command_chain']=command_outcome
        body['explanation']['warning_refs']=[{k:r[k] for k in ('warning_id','receipt_id','name','service','occurred_at','content','location')}
            for r in command_outcome['result']['items']]
    if cmd.action=='select_plan' and source and (source.get('explanation') or {}).get('attribution_v2'):
        from ..dialogue.plan_projection import preserve
        p=next((p for p in source['explanation']['attribution_v2']['problems'] if p['problem_id']==task.problem_id),None)
        preserve(body,source,p)
    return body
