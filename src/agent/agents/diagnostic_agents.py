"""Source-oriented deterministic forensics, not one model per exception."""
from ..common.enums import AGENT_TOOL_MATRIX,EvidenceSource
from .base import ForensicsAgent,ToolRequest,target_services
from ..diagnostics.evidence import from_event


class DiagnosticAgent(ForensicsAgent):
    source=EvidenceSource.CHANGES
    initial_tool=''

    def plan_calls(self,ctx):
        if not selected(self.role,ctx.slots.symptom or ''):return []
        params=dict(ctx.query_window(),env=ctx.slots.env or 'prod')
        if ctx.slots.cluster:params['cluster']=ctx.slots.cluster
        return [ToolRequest(tool=self.initial_tool,params=dict(params,service=service,limit=100),
            signal=self.initial_tool,rationale='按目标/窗口读取独立诊断观测；空记录不证明正常')
            for service in target_services(ctx)]

    def build_evidence(self,ctx,request,result):
        return [from_event(row,self.role,result.raw_ref,(ctx.window_start,ctx.window_end))
                for row in (result.data or {}).get('events',[])]

    def summarize_empty(self,request,result):
        return '没有留存观测；覆盖待核实：'+request.tool


class DatabaseAgent(DiagnosticAgent):
    role='database_agent';tools=AGENT_TOOL_MATRIX[role];initial_tool='db_activity_read'


class RuntimeAgent(DiagnosticAgent):
    role='runtime_agent';tools=AGENT_TOOL_MATRIX[role];initial_tool='runtime_snapshot_read'

    def plan_calls(self,ctx):
        requests=super().plan_calls(ctx)
        from ..observed.context import current
        if current():requests=[ToolRequest(tool='sdk_snapshot_read',params={'service':s},signal='sdk_current_snapshot',
            rationale='按需访问已登记 SDK；当前快照不作为历史原因证明',purpose='aux') for s in target_services(ctx)]+requests
        return requests


class LifecycleAgent(DiagnosticAgent):
    role='lifecycle_agent';tools=AGENT_TOOL_MATRIX[role];initial_tool='process_event_read'

    def build_evidence(self,ctx,request,result):
        from ..diagnostics.restarts import evidence
        rows=(result.data or {}).get('events',[])
        return super().build_evidence(ctx,request,result)+evidence(rows,result.raw_ref,(ctx.window_start,ctx.window_end))


def diagnostic_agents():
    import os
    roles=os.getenv('AIOPS_DIAGNOSTIC_ROLES','database_agent,runtime_agent,lifecycle_agent').split(',')
    return [a for a in (DatabaseAgent(),RuntimeAgent(),LifecycleAgent()) if a.role in roles]


def selected(role,symptom):
    text=symptom.lower()
    if not text:return True
    if role=='runtime_agent':return True
    if role=='database_agent':return any(t in text for t in ('sql','数据库','事务','死锁','连接池','migration','ddl','表不存在'))
    return any(t in text for t in ('崩溃','退出','重启','不可达','整个系统','所有服务','系统怎么样','crash','restart'))


async def after_discovery(runner,ctx,merged,on_event):
    """Only source gaps discovered in phase one unlock additional roles."""
    text=' '.join(e.summary+' '+' '.join(e.tags[:8]) for o in merged.values() for e in o.evidence)
    wanted=set()
    if any(t in text.lower() for t in ('sql','数据库','db_deadlock','pool_acquire')):wanted.add('database_agent')
    if any(t in text.lower() for t in ('process_exit','process_start_failed')):wanted.add('lifecycle_agent')
    agents=[a for a in runner.agents if a.role in wanted and a.role not in merged]
    for agent in agents:
        # Use the same source preflight, event path and hard attempt budget.
        from dataclasses import replace
        from .base import AgentOutcome
        follow=replace(ctx,slots=ctx.slots.model_copy(update={'symptom':''}))
        plan=runner.planner.plan(follow,[agent])
        from .admission import admit
        admit(plan)
        await runner._record_plan(ctx.incident_id,plan)
        for n,call in enumerate(plan.calls):
            outcome=await runner._one_call(agent,follow,call,'diagnostic-followup-'+agent.role+str(n),on_event)
            merged.setdefault(agent.role,AgentOutcome(role=agent.role)).merge(outcome)
