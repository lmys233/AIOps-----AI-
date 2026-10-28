"""One command/transition owner for registered-system turns."""
from ..domain.tasks import begin,Conflict
from .ports import TaskPorts

class TaskController:
    def __init__(self,repository,ports: TaskPorts | None = None):
        self.repository,self.ports=repository,ports

    def dependencies(self, service):
        ports=self.ports or service.task_ports
        if ports is None:raise RuntimeError("TaskPorts not configured")
        return ports

    async def handle(self,service,request,scope,trace_id,usage,on_event,incident_id):
        ports=self.dependencies(service)
        with ports.context(request,scope) as context:
            return ports.context_result(await self._handle(service,request,scope,trace_id,usage,on_event,incident_id),context)

    async def _handle(self,service,request,scope,trace_id,usage,on_event,incident_id):
        ports=self.dependencies(service)
        task=None;cmd=None;execution=None
        try:
            tasks,meta=await self.repository.load(scope)
            if not meta['imported']:
                await self.repository.import_once(scope,await ports.import_tasks(scope))
                tasks,meta=await self.repository.load(scope)
            target=request.context.dialogue
            if target and target.task_id:
                exact=await self.repository.get(scope,target.task_id)
                if exact:tasks=[exact]+[t for t in tasks if t.task_id!=exact.task_id]
            task,cmd,extra=await ports.choose(request,scope,tasks,meta['foreground'])
            tracked=cmd.action in ('new_task','fill_scope','probe_endpoint','reinvestigate','select_plan','verify_target','request_plan','resume_execution','answer_followup')
            from uuid import uuid4
            store=getattr(self.repository,'executions',None) if tracked else None
            recovery=None
            if cmd.action=='resume_execution':
                execution=await ports.prepare(store,task,cmd.turn_id,usage,resume=True)
                recovery={'execution_id':execution.rid}
            elif tracked:
                task=task.model_copy(update={'execution_id':'exe_'+uuid4().hex})
                task.memory={**task.memory,'goal':task.original,'purpose':task.purpose,'slots':task.slots,
                    'execution_id':task.execution_id}
            before=task.revision;task=begin(task,cmd,scope,recovery=recovery)
            if tracked and execution is None:execution=await ports.prepare(store,task,cmd.turn_id,usage)
            event=dict(event_id=request.session_id+':'+cmd.turn_id+':start',phase='start',**cmd.model_dump())
            if execution:await self.repository.start(task,before,meta['revision'],event,execution=execution)
            else:await self.repository.start(task,before,meta['revision'],event)
            ports.bind_task(task,cmd)
        except Exception as exc:
            # prepare owns no lease; a failed atomic start rolls back the claim.
            return ports.refusal(request,trace_id,exc)
        from ..common.steps import emit_step
        emit_step(on_event,'task_control','确定任务与本轮动作',
            {'task_id':task.task_id,'action':cmd.action,'purpose':task.purpose,'revision':task.revision},result=cmd.action)
        try:
            from contextlib import nullcontext
            async with execution.activate() if execution else nullcontext():
                if cmd.action=='resume_execution':
                    emit_step(on_event,'execution_resume','恢复中断执行',execution.summary(),result='保留原查询窗口；跳过已完成节点，复用节点内已完成调用')
                body=await ports.execute(request,task,cmd,extra,trace_id,usage,on_event,incident_id)
                if execution:body.setdefault('explanation',{})['execution']=execution.summary()
        except BaseException as exc:
            if execution:await execution.release()
            if not isinstance(exc,Exception):raise
            body=ports.refusal(request,trace_id,exc)
            if execution:body.setdefault('explanation',{})['execution']={**execution.summary(),'interrupted':True}
            if execution:
                info=execution.summary()
                memory=execution.body.get('task_memory') or {}
                body['explanation']['task_memory']=memory
                if memory.get('purpose'):body['intent']['name']=memory['purpose']
                if memory.get('slots'):body['slots']=memory['slots']
                message=('执行中断，台账写入失败，不能保证最新进度已保存；已停止新增调用。' if execution.fenced else
                    '执行中断，进度已保存；模型费用或结果未知，不能重置预算继续，请明确开始新任务。' if info['budget_unknown'] else
                    '执行中断，进度已保存。可回复“继续任务”，从未完成节点恢复；已完成工具不会重新请求上游。')
                body['report']=message;body['explanation']['answer']['summary']=message
        else:
            if execution:await execution.release(status='ready')
        if ports.state_note and ports.state_note not in body['degraded']:body['degraded'].append(ports.state_note)
        info=body.get('explanation',{}).get('execution')
        if info and info.get('recoveries'):
            answer=body['explanation'].get('answer') or {}
            answer.setdefault('details',[]).append({'label':'执行恢复',
                'value':f"执行 {info['execution_id']}；恢复 {info['recoveries']} 次；复用 {info['reused_calls']} 条调用；原查询窗口截止 {info['query_anchor']}。复用历史结果不能证明此刻状态。"})
        return self.stamp(body,task,cmd)

    def stamp(self,body,task,cmd):
        ex=body.setdefault('explanation',{});d=ex.setdefault('dialogue',{})
        d.update(task_id=task.task_id,revision=str(task.revision+1),action=cmd.action,
            based_on_revision=str(cmd.based_on_revision),task_used=task.used,
            based_on_incident=task.last_incident_id,problem_id=cmd.problem_id or d.get('problem_id',''))
        ex['task_control']={'task_id':task.task_id,'execution_revision':task.revision,
            'revision':task.revision+1,'action':cmd.action,'purpose':task.purpose,
            'turn_id':cmd.turn_id,'observed_id':task.scope.observed_id,'commit_state':'pending'}
        if cmd.action=='fill_scope':
            body['resumed_task']={'task_id':task.task_id,'from':task.original[:80],
                'note':'结构化补充任务范围；保留原任务目的，不重新识别意图'}
        return body

    async def commit(self,service,request,scope,body,details):
        ports=self.dependencies(service)
        control=body['explanation']['task_control']
        old=await self.repository.get(scope,control['task_id'])
        if not old or old.revision!=control['execution_revision'] or old.run_id!=request.turn_id:
            raise Conflict('执行版本已变化，未覆盖任务')
        task=ports.project(old,body);task.revision+=1
        body['explanation']['task_control'].update(purpose=task.purpose,state=task.state,commit_state='committed')
        event=dict(event_id=request.session_id+':'+request.turn_id+':finish',action=control['action'],phase='finish',incident_id=body['incident_id'])
        await ports.invalidate(request.session_id,body)
        await self.repository.finish(task,old.revision,event,ports.persisted_payload(request,body,details))
        await ports.cache_result(body)
        await ports.invalidate(request.session_id,body,annotate=False)
