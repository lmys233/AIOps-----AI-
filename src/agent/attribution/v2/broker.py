"""沿现有执行器权限、审计和黑板发表定向检查；不把模型解释变成事实。"""
from __future__ import annotations
import dataclasses
import asyncio
import json
import time
from ...board.store import new_evidence_id
from ...agents.base import AgentOutcome
from ...common.contracts import Evidence, EntityRef, ToolResult
from ...common.steps import emit_step_phase
from ...tools.handlers import ToolContext
from ...common.enums import EvidenceSource, EvidenceDirection, ToolPermission
from ...orchestration.state import board_store
from ...application.diagnosis_workflow import Fetched
from .verification import Observation
from ...tools.availability import check_availability, safe_detail


class Broker:
    def __init__(self, orchestrator, state):
        self.state,self.owner=state,orchestrator
        self.snapshot=None
        self.board=board_store(state,orchestrator.board,need='get_evidence')
        executor=orchestrator.executor
        context=dataclasses.replace(executor.context or ToolContext(),board=self.board,
                                    incident_id=state.incident_id)
        self.executor=executor.with_context(context)
        self.definitions={}
        self.roles={}
        self.adapters={}
        from ...queries.capabilities import view
        for name,capability in view(executor.registry).items():
            self.definitions[name]={**capability.schema,
                'x-aiops-verification-records':capability.definition.verification_records,
                'x-aiops-description':capability.definition.spec.description}
            self.roles[name]=capability.roles[0]
            self.adapters[name]=capability.definition

    def availability(self,tool,params):
        if not self.adapters[tool].verification_records:
            from ...tools.availability import Availability
            return Availability('unsupported','该工具返回普通观测，不能直接核实命名因果条件',
                '沿既有取证保留原始观测；接入相应的独立语义验证记录，不能重复查询相同指标充当因果证明')
        return check_availability(self.adapters[tool].availability,params)

    def emit(self,event,data):
        self.owner._emit(self.state,event,data)

    async def fetch(self,query):
        role=self.roles[query.tool]
        started=time.monotonic()
        spec=query.query_spec=self.query_spec(query)
        self.phase(query,'start')
        from ...common.tool_budget import tool_budget_scope
        try:
            with tool_budget_scope(self.state.budget,pre_reserved=True):
                result=await self.executor.call_query(role,spec) if spec else await self.executor.call(role,query.tool,query.params)
        except asyncio.CancelledError:
            self.phase(query,'done',ToolResult(ok=False,degraded=True,error_code='TOOL_UNAVAILABLE',
                error_type='CancelledError',summary='本次核查被中断，未取得完整结果；不能据此判断 SDK 或业务故障'),time.monotonic()-started)
            raise
        self.phase(query,'done',result,time.monotonic()-started)
        if not result.ok:return self.failed(query,self.failure(result),str(result.error_code or ''),safe_detail(result.raw_ref))
        # 普通日志/指标没有命名因果字段，保留原始引用；不能凭一段摘要证明必要条件。
        records=(result.data or {}).get('records',[]) if isinstance(result.data,dict) else []
        if not records or not self.adapters[query.tool].verification_records:
            return self.unverified(query,'查询已完成，但观测不足以核实该候选；需关联事务/任务或相应历史记录')
        rows,readable=[],set()
        for record in records:
            row=Observation.model_validate(record)
            row.reason=safe_detail(row.reason)
            if result.data.get('truncated'):
                row.complete=False
                row.reason+='；返回记录已截断，不能认定验证覆盖完整'
            item=self.evidence(row,result.raw_ref,role)
            await self.board.publish(self.state.incident_id,role,[item])
            self.state.outcomes.append(AgentOutcome(role=role,evidence=[item]))
            saved=await self.board.get_evidence(self.state.incident_id,item.evidence_id)
            if saved is not None and saved.tags==item.tags and saved.raw_ref==item.raw_ref:
                readable.add(item.evidence_id)
            rows.append(row)
        return Fetched(rows,readable,raw_ref=safe_detail(result.raw_ref))

    def query_spec(self,query):
        from ...observed.context import current
        from ...queries.binding import bind,scope_of
        from ...domain.queries import QueryScope,QueryWindow
        if not self.snapshot or not query.requirements:
            scope=scope_of(current(),[query.params['service']] if query.params.get('service') else ())
            scope=scope or QueryScope(tenant='default',env=query.params.get('env') or 'prod',services=tuple(filter(None,[query.params.get('service')])))
            return bind(scope,query.tool,query.params,self.adapters[query.tool],signal='candidate_verification')
        problem=next(p for p in self.snapshot.problems if p.problem_id==query.requirements[0][0])
        scope=scope_of(current(),[problem.service]) or QueryScope(tenant='default',env=problem.env,
            cluster=problem.cluster or None,services=(problem.service,))
        requested=QueryWindow(start=problem.time_range[0],end=problem.time_range[1]) if problem.time_range[0]<problem.time_range[1] else None
        return bind(scope,query.tool,query.params,self.adapters[query.tool],signal='candidate_verification',requested=requested)

    def phase(self,query,phase,result=None,elapsed=0):
        def enriched(event,data):
            data.update(why=query.rationale,elapsed_ms=round(elapsed*1000,1))
            if query.query_spec:data['query_spec']=query.query_spec.summary()
            if result is not None:
                data.update(raw_ref=safe_detail(result.raw_ref),error_code=str(result.error_code or ''))
            self.emit(event,data)
        emit_step_phase(enriched,phase,query.tool,'定向只读核查',query.params,
            call_id=query.query_id,role=self.roles[query.tool],ok=result.ok if result else True,
            result=safe_detail(result.summary) if result and result.ok else self.failure(result) if result else '')

    @staticmethod
    def failure(result):
        return safe_detail('核查失败：'+str(result.error_code)+'；'+str(result.summary))

    def failed(self,query,reason,error_code='',raw_ref=''):
        rows=[]
        for pid,cid,key in query.requirements:
            p=next(p for p in self.snapshot.problems if p.problem_id==pid)
            c=next(c for c in p.candidates if c.candidate_id==cid)
            r=next(r for r in c.requirements if r.key==key)
            rows.append(Observation(signal=r.signal,service=p.service,object_name=p.object_name,
                env=p.env,cluster=p.cluster,time_range=p.time_range,failed=True,reason=reason))
        return Fetched(rows,set(),error_code,raw_ref)

    def unverified(self,query,reason):
        result=self.failed(query,reason)
        for row in result.observations:row.failed=False
        return result

    @staticmethod
    def evidence(row,raw_ref,role):
        row.evidence_id=new_evidence_id()
        return Evidence(evidence_id=row.evidence_id,source=EvidenceSource.CHANGES,agent_role=role,
            entity=EntityRef.make('service',row.service,cluster=row.cluster),
            signal=row.signal,direction=EvidenceDirection.FLAT,time_range=row.time_range,
            onset=row.time_range[0],confidence=1 if row.scope_verified else 0,
            raw_ref=safe_detail(raw_ref),summary=row.signal+'='+str(row.value)+'；'+row.reason,
            tags=['attribution_v2_check','observation_json='+json.dumps(row.model_dump(mode='json'),ensure_ascii=False)])
