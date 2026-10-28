"""Composition boundary. Domain never discovers infrastructure or ambient scope."""
import os
from ..domain.tasks import Scope


def enabled(service):
    from ..observed.context import current
    # Verified registered scope has one execution owner. Missing storage is a
    # capability failure, never permission to use the old untracked writer.
    return current() is not None


def controller(service):
    owner=getattr(service,'_task_controller',None)
    if owner:return owner
    from ..application.task_controller import TaskController
    from ..adapters.task_repository import PgTasks
    repo=getattr(service,'task_repository',None) or PgTasks(service.history.store)
    service._task_controller=TaskController(repo,service.task_ports)
    return service._task_controller


def executions(service):
    store=getattr(service,'execution_repository',None)
    if store is None:
        from ..execution.store import PgExecutions
        store=PgExecutions(service.history.store);service.execution_repository=store
    return store


def scope_for(request):
    from ..observed.context import current
    scope=current()
    if not scope:raise PermissionError('任务控制需要已核实系统作用域')
    return Scope(tenant=scope.tenant,observed_id=scope.observed_id,session_id=request.session_id,
        env=scope.env,cluster=scope.cluster,services=list(scope.names))


async def reference_incident(service,request,target):
    from ..domain.tasks import Scope
    tenant=os.getenv('AIOPS_TENANT','default');oid=request.context.observed_id
    if not oid:
        from ..observed.workspace import store_of
        row=await store_of(service).one('SELECT observed_id FROM agent_system_sessions WHERE tenant=$1 AND session_id=$2',tenant,request.session_id)
        oid=row['observed_id'] if row else None
    if not oid:raise ValueError('任务恢复需指定被观测系统')
    scope=Scope(tenant=tenant,observed_id=oid,session_id=request.session_id,env=request.context.env or 'prod')
    task=await controller(service).repository.get(scope,target)
    if task is None:raise ValueError('任务不属于当前会话/系统或已不可读')
    return task.last_incident_id
