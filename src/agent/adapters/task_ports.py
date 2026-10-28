"""HTTP/graph adapter for TaskPorts; API internals remain at this boundary."""
from . import task_context, task_output
from .task_legacy import import_tasks
from .task_projection import project
from .task_execution import execute
from .task_choice import choose


class ServiceTaskPorts:
    def __init__(self, service): self.service = service
    @property
    def state_note(self): return self.service.state_note
    def context(self, request, scope): return task_context.bind(self.service, request, scope)
    def context_result(self, body, context): return task_context.stamp(body, context)
    def bind_task(self, task, command):
        from ..llm.context import CURRENT
        CURRENT.get().bind(task, command)
    async def import_tasks(self, scope): return await import_tasks(self.service, scope)
    async def choose(self, request, scope, tasks, foreground):
        return await choose(self.service, request, scope, tasks, foreground)
    async def prepare(self, repository, task, turn_id, usage, *, resume=False):
        from ..execution.runtime import Execution
        from .task_runtime import executions
        return await Execution.prepare(repository or executions(self.service), task, turn_id, usage, resume=resume)
    async def execute(self, request, task, command, extra, trace_id, usage, on_event, incident_id):
        return await execute(self.service, request, task, command, extra, trace_id, usage, on_event, incident_id)
    def refusal(self, request, trace_id, error): return task_output.refusal(request, trace_id, error)
    def project(self, task, body): return project(task, body)
    def persisted_payload(self, request, body, details): return task_output.persisted_payload(request, body, details)
    async def invalidate(self, session_id, body, *, annotate=True):
        await task_output.invalidate(self.service, session_id, body, annotate=annotate)
    async def cache_result(self, body): await task_output.cache_result(self.service, body)

    async def store_incident(self, body): return await self.service._store_incident(body)
    async def answer_step(self, *args): return await self.service._answer_step(*args)
    def render(self, *args, **kwargs): return self.service._render(*args, **kwargs)
    async def settle_turn(self, *args): return await self.service._settle_turn(*args)
