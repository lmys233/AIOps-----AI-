"""Choose a command once; NLP providers propose, controller validates membership."""
from ..domain.tasks import Task,Command
from ..adapters.task_route import patch_for
from ..adapters import task_dialogue as dialogue


async def source_rows(service,tasks):
    from ..adapters.task_projection import source_for
    from ..observed.context import body_scope
    rows=[]
    for task in tasks[:8]:
        if not task.diagnosis_id:continue
        body=await service.get_incident(task.diagnosis_id)
        if body and body_scope(body)==task.scope.observed_id and body.get('session_id')==task.scope.session_id:
            from ..adapters.task_sources import recover
            body=await recover(service,body,task)
            rows.append(source_for(task,body))
    return rows


async def selected_plan(service,task,message):
    if not task.plans:return None
    from ..adapters.task_projection import shown_plans
    from ..remediation.plans import plan_from_ledger
    body=await service.get_incident(task.plan_source_id)
    from ..observed.context import body_scope
    if not body or body_scope(body)!=task.scope.observed_id or body.get('session_id')!=task.scope.session_id:return None
    return plan_from_ledger([{'plans':shown_plans(body)}],message)


def command(task,request,action,**fields):
    return Command(task_id=task.task_id,based_on_revision=task.revision,
        turn_id=request.turn_id,action=action,**fields)


def requested_task(request,tasks):
    target=request.context.dialogue
    if not target or not target.task_id:return None
    task=next((t for t in tasks if target.task_id in (t.task_id,t.last_incident_id,t.legacy_id)),None)
    if not task:raise ValueError('任务不属于当前作用域或已不可读')
    if target.revision and target.revision not in (str(task.revision),task.last_incident_id):
        raise ValueError('任务版本已变化，请基于最新记录继续')
    return task


def requery(service,request,task,hint):
    if not hint or hint.action!='new_task' or hint.reason!='显式重新取证' or not task or not task.purpose:return False
    import re
    if re.search(r'新任务|\bnew task\b',request.message,re.I):return False
    from ..intent.rules import match_rules
    hit=match_rules(request.message,slots_config=service.router.slot_config)
    return hit.is_fallback or hit.intent.value==task.purpose


async def unfinished(service,task):
    """Unknown accounting limits new work, but does not undo a committed execution."""
    from .task_runtime import controller,executions
    repo=controller(service).repository
    store=getattr(repo,'executions',None) or executions(service)
    run=await store.read(task.scope,task.execution_id)
    if run is None:raise ValueError('执行台账不可读；未猜测中断状态或重放调用')
    return run.get('status')!='completed'


async def choose(service,request,scope,tasks,foreground):
    from ..commands.parsing import has_commands
    if has_commands(request.message):
        task=Task(scope=scope,original=request.message,purpose='INCIDENT_RCA')
        return task,command(task,request,'new_task'),{'command_chain':True}
    from ..intent.augment import _explicit_request
    from ..dialogue.routing import quick
    active=[t for t in tasks if t.scope.same(scope)]
    explicit=requested_task(request,active)
    first=explicit or next((t for t in active if t.task_id==foreground),None)
    if explicit:active=[explicit]+[t for t in active if t.task_id!=explicit.task_id]
    import re
    from ..intent.routing_policy import routing_input
    continuation=re.fullmatch(r'(?:请|帮我)?(?:继续|恢复|接着)(?:(?:之前|上次|刚才|中断|之前中断)(?:的)?\s*)?(?:执行|排查|任务|诊断|工作)?[。！!？?\s]*',routing_input(request.message))
    if continuation and first and first.execution_id and (first.state=='investigating' or first.budget_unknown):
        if await unfinished(service,first):
            return first,command(first,request,'resume_execution'),{}
        # A completed run with unknown billing cannot be replayed or granted a fresh
        # budget by saying "continue". Explain its saved result without new queries.
        rows=await source_rows(service,[first])
        if not rows:raise ValueError('执行已完成，但留存诊断不可读；未重放已完成执行')
        return first,command(first,request,'explain_result'),dict(decision=quick('解释结果'),source=rows[0],task_wide=True)
    if request.context.task_patch:
        if first is None:raise ValueError('没有可恢复的待补范围任务')
        target=request.context.dialogue
        if target and (target.task_id not in ('',first.task_id,first.legacy_id,first.last_incident_id) or target.revision and target.revision!=str(first.revision)):
            raise ValueError('任务引用或版本不一致')
        return first,command(first,request,'fill_scope',patch=request.context.task_patch.model_dump(exclude_unset=True)),{}
    if first:
        plan=await selected_plan(service,first,request.message)
        if plan:return first,command(first,request,'select_plan',option_id=plan.option_id),{'plan':plan}
    from .task_answer import propose as answer_followup
    answer_context=await answer_followup(service,request,first)
    if answer_context:
        return first,command(first,request,'answer_followup',patch=answer_context['patch']),{'answer_context':answer_context}
    if first and first.memory.get('active_probe') and first.memory['active_probe'].get('action')=='ask':
        from ..webprobe.planning import follows
        try:follow=await follows(service,request,first)
        except Exception:follow=False
        if follow:
            data={'message':request.message}
            pending=first.memory['active_probe'].get('pending')
            from ..intent.routing_policy import routing_input
            if pending and routing_input(request.message).strip()=='确认探测 '+pending['params']['run_id']:
                data['approved']=pending
            return first,command(first,request,'probe_endpoint'),{'probe_input':data}
    hint=quick(request.message)
    if not active and not explicit:
        # A dialogue verb alone cannot invent an incident in an empty session.
        # Explicit ID references keep their existing target clarification path.
        from ..dialogue.routing import selection_ref
        if not selection_ref(request.message):
            task=Task(scope=scope,original=request.message)
            return task,command(task,request,'new_task'),{}
    if hint and hint.action in ('explain_gaps','explain_result') and first and first.execution_id and (first.state=='investigating' or first.budget_unknown) and await unfinished(service,first):
        return first,command(first,request,hint.action),{'execution_info':True}
    if hint and hint.action in ('explain_gaps','explain_result') and first and not first.diagnosis_id and first.last_incident_id:
        source=await service.get_incident(first.last_incident_id)
        from ..observed.context import body_scope
        if source and body_scope(source)==first.scope.observed_id and source.get('session_id')==first.scope.session_id:
            from ..adapters.task_projection import source_for
            return first,command(first,request,hint.action),dict(decision=hint,source=source_for(first,source),task_wide=True)
    if requery(service,request,first,hint):
        return first,command(first,request,'reinvestigate',patch=patch_for(request.message,service.router,first)),{}
    if not hint and not _explicit_request(request.message,service.router.slot_config):
        for task in active:
            if 'service' not in task.missing:continue
            patch=patch_for(request.message,service.router,task)
            if patch.get('service_names'):return task,command(task,request,'fill_scope',patch=patch),{}
    rows=await source_rows(service,active)
    if first and first.pending:
        rows.sort(key=lambda b:((b.get('explanation') or {}).get('dialogue') or {}).get('task_id')!=first.task_id)
    proposal=hint or (await dialogue.propose(service,request,rows) if rows else None)
    if proposal and proposal.action!='new_task':
        if explicit:proposal.task_id=explicit.task_id;proposal.revision=str(explicit.revision)
        from ..adapters.task_information import task_source
        wide=task_source(rows,request,proposal)
        if wide:
            task=next(t for t in active if t.task_id==wide['explanation']['dialogue']['task_id'])
            return task,command(task,request,proposal.action),dict(decision=proposal,source=wide,p=None,rows=rows,reason='',task_wide=True)
        source,p,reason=dialogue.resolve(rows,request,proposal,scope.services)
        key=((source or {}).get('explanation',{}).get('dialogue') or {}).get('task_id')
        task=next((t for t in active if t.task_id==key),None) or first or Task(scope=scope,original=request.message,purpose='INCIDENT_RCA')
        fields=dict(problem_id=p['problem_id'] if p else '',candidate_id=proposal.candidate_id if p else '',requirement_key=proposal.requirement_key)
        cmd=command(task,request,proposal.action,**fields)
        return task,cmd,dict(decision=proposal,source=source,p=p,rows=rows,reason=reason)
    task=Task(scope=scope,original=request.message)
    return task,command(task,request,'new_task'),{}
