from typing import Literal
from uuid import uuid4
from pydantic import BaseModel,ConfigDict,Field,field_validator
from ..common.enums import Intent

Action=Literal['new_task','reinvestigate','resume_execution','fill_scope','probe_endpoint','select_plan','explain_result','explain_gaps','answer_followup',
    'submit_evidence','compare_candidates','verify_target','request_plan','clarify_target','confirmation']
State=Literal['investigating','awaiting_scope','awaiting_target','awaiting_evidence',
    'ready_for_plan','suspended','completed','failed']


class Record(BaseModel):
    model_config=ConfigDict(extra='forbid',validate_assignment=True)


class Scope(Record):
    tenant:str
    observed_id:str
    session_id:str
    env:str
    cluster:str|None=None
    services:list[str]=Field(default_factory=list)

    @field_validator('cluster',mode='before')
    @classmethod
    def optional_cluster(cls,value):return value or None

    def same(self,other):
        return (self.tenant,self.observed_id,self.session_id,self.env,self.cluster)==(
            other.tenant,other.observed_id,other.session_id,other.env,other.cluster)


class PlanRef(Record):
    option_id:str
    title:str
    action_class:str=''


class ScopePatch(Record):
    service_names:list[str]|None=Field(default=None,max_length=50)
    service:dict|None=None
    time_window:str|None=Field(default=None,pattern=r'^\d+[smhd]$')
    env:str|None=None
    cluster:str|None=None

    @field_validator('cluster',mode='before')
    @classmethod
    def optional_cluster(cls,value):return value or None


class Task(Record):
    task_id:str=Field(default_factory=lambda:'tsk_'+uuid4().hex,pattern=r'^tsk_[a-f0-9]{32}$')
    scope:Scope
    purpose:str|None=None
    original:str=Field(default='',max_length=4000)
    state:State='investigating'
    revision:int=Field(default=0,ge=0)
    slots:dict=Field(default_factory=dict)
    missing:list[str]=Field(default_factory=list)
    diagnosis_id:str=''
    last_incident_id:str=''
    legacy_id:str=''
    problem_ids:list[str]=Field(default_factory=list)
    candidate_refs:dict[str,list[str]]=Field(default_factory=dict)
    problem_id:str=''
    candidate_id:str=''
    pending:dict=Field(default_factory=dict)
    plan_source_id:str=''
    plans:list[PlanRef]=Field(default_factory=list)
    selected_plan_id:str=''
    used:dict[str,float]=Field(default_factory=dict)
    run_id:str=''
    budget_unknown:bool=False
    execution_id:str=''
    memory:dict=Field(default_factory=dict)

    @field_validator('purpose')
    @classmethod
    def known_purpose(cls,value):
        if value is not None:Intent(value)
        return value


class Command(Record):
    action:Action
    task_id:str
    based_on_revision:int=Field(ge=0)
    turn_id:str
    patch:dict=Field(default_factory=dict)
    problem_id:str=''
    candidate_id:str=''
    requirement_key:str=''
    option_id:str=''


class Conflict(ValueError):pass


def begin(task,command,scope,*,recovery=None):
    if not task.scope.same(scope):raise PermissionError('任务系统/环境与当前作用域不一致')
    if command.task_id!=task.task_id or command.based_on_revision!=task.revision:
        raise Conflict('任务版本已变化，请基于最新记录继续')
    unknown=task.budget_unknown or bool(task.revision and task.state=='investigating' and task.run_id)
    if command.action=='resume_execution':
        if not recovery or recovery.get('execution_id')!=task.execution_id or not task.execution_id:
            raise Conflict('恢复执行需要服务端核实的台账与租约')
        unknown=False
    if unknown and command.action not in ('explain_result','explain_gaps','clarify_target'):
        raise Conflict('上一轮未落定，累计预算未知；可查看旧诊断，或明确开始新任务')
    if command.action=='fill_scope' and not task.missing:raise Conflict('任务没有待补槽位')
    if command.action=='probe_endpoint' and not task.memory.get('active_probe'):
        raise Conflict('任务没有待接续的主动核验')
    if command.action=='fill_scope' and 'service' in task.missing and not (command.patch.get('service_names') or command.patch.get('service')):
        raise ValueError('仍未补上服务范围')
    if command.action=='fill_scope' and set(command.patch)-{'service_names','service','time_window','env','cluster'}:
        raise ValueError('范围补充包含非槽位字段')
    if not set(command.patch.get('service_names',[]))<=set(scope.services):
        raise PermissionError('补充了未登记服务')
    service=command.patch.get('service')
    if service and (service.get('name') if isinstance(service,dict) else str(service)) not in scope.services:
        raise PermissionError('补充了未登记服务实体')
    if command.patch.get('env',scope.env)!=scope.env or command.patch.get('cluster',scope.cluster)!=scope.cluster:
        raise PermissionError('补槽不能暗中切换环境/集群')
    if command.problem_id and command.problem_id not in task.problem_ids:
        raise ValueError('问题不属于当前任务')
    if command.candidate_id and command.candidate_id not in task.candidate_refs.get(command.problem_id or task.problem_id,[]):
        raise ValueError('候选不属于所选问题')
    effective={**task.slots,**command.patch}
    if command.action in ('resume_execution','fill_scope','probe_endpoint','reinvestigate','verify_target','request_plan','select_plan','answer_followup') and not set(effective.get('service_names') or [])<=set(scope.services):
        raise PermissionError('任务查询目标已停用或不在登记范围')
    if command.action=='select_plan' and command.option_id not in {p.option_id for p in task.plans}:
        raise ValueError('方案不属于已展示选项')
    value=task.model_copy(deep=True);value.revision+=1;value.run_id=command.turn_id
    value.budget_unknown=unknown
    value.state='investigating';value.slots.update(command.patch)
    value.problem_id=command.problem_id or value.problem_id
    value.candidate_id=command.candidate_id or value.candidate_id
    if command.action=='select_plan':value.selected_plan_id=command.option_id
    return value
