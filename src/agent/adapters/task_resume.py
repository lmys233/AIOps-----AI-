"""Transport adapter: clarification becomes a slot patch, never synthesized prose."""
from ..domain.tasks import ScopePatch


async def clarification(service,payload,sid,trace_id,answers,plugin_id):
    from ..api.requests import ChatRequest
    from ..adapters.task_runtime import scope_for,controller
    from ..adapters.task_route import patch_for
    request=ChatRequest(session_id=sid,message='补充任务范围',user={'id':str(payload.get('user_id') or 'human:unknown')},
        context={**dict(payload.get('context') or {}),'observed_id':payload.get('observed_id')})
    scope=scope_for(request);tasks,meta=await controller(service).repository.load(scope)
    from ..application.task_choice import requested_task
    target=request.context.dialogue
    if target and target.task_id:
        exact=await controller(service).repository.get(scope,target.task_id)
        if exact:tasks=[exact]+[t for t in tasks if t.task_id!=exact.task_id]
    chosen=requested_task(request,tasks)
    task=chosen or next((t for t in tasks if t.task_id==meta['foreground']),None)
    if task is None:raise ValueError('没有可恢复的任务，请先指定目标')
    data=answers.get(plugin_id,answers)
    if isinstance(data,str):data={'service':data}
    if not isinstance(data,dict):raise ValueError('澄清答案应是结构化槽位')
    if set(data)-{'time_window','env','cluster','service_names','service'}:
        raise ValueError('本恢复入口仅接受范围槽位；问题、候选或方案请在对话中显式选择')
    patch={k:v for k,v in data.items() if k in ('time_window','env','cluster','service_names')}
    if data.get('service'):
        name=data['service'].get('name','') if isinstance(data['service'],dict) else str(data['service'])
        patch.update(patch_for(name,service.router,task))
        if not patch.get('service_names'):raise ValueError('服务未登记或名称无法核实')
    typed=ScopePatch.model_validate(patch)
    keys={'service' if k in ('service','service_names') else k for k in patch}
    if chosen is None:task=next((t for t in tasks if t.scope.same(scope) and set(t.missing)&keys),task)
    request=request.model_copy(update={'context':request.context.model_copy(update={'env':scope.env,
        'cluster':scope.cluster,'task_patch':typed,'dialogue':target_for(task)})})
    return await service.handle(request,trace_id=trace_id)


def target_for(task):
    from ..dialogue.contracts import Target
    return Target(task_id=task.task_id,revision=str(task.revision))
