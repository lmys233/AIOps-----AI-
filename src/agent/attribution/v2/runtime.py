"""V2 请求适配；顶层旧根因只投影单个已确认问题，关闭开关保留旧链路。"""
from ...common.contracts import ArbitResult, Hypothesis, EntityRef
from ...common.enums import ArbitDecision, HypothesisStatus
from ...common.steps import emit_step_phase
from ...orchestration.usage import sync_usage
from .facts import build_problems
from .contracts import Snapshot, project_root
from .engine import run
from .broker import Broker


async def diagnose(owner,state):
    broker=Broker(owner,state)
    from ...observed.current_access import publish as publish_access
    await publish_access(state,broker.board)
    emitter=lambda event,data:owner._emit(state,event,data)
    emit_step_phase(emitter,'start','hypotheses','按问题生成候选与验证',
                    {'evidence':len(state.evidence)})
    scope=list(state.slots.service_names or [])
    if state.slots.service:scope.insert(0,state.slots.service.name)
    problems=build_problems(state.evidence,env=state.slots.env or '',services=scope)
    snapshot=business_snapshot(problems,state.flags.get('sdk_verification') or {})
    priorities=state.flags.get('metric_localization')
    if priorities:
        snapshot.source['metric_localization']={k:v for k,v in priorities.items() if k!='profiles'}
    from ...diagnostics.presentation import annotate
    annotate(snapshot,state.evidence)
    from ...observed.access_status import capture as capture_access
    from ...observed.context import current
    capture_access(snapshot,state.evidence,current(),state.incident_id)
    readable=set()
    for e in state.evidence:
        if broker.board and await broker.board.get_evidence(state.incident_id,e.evidence_id):
            readable.add(e.evidence_id)
    from ...diagnostics.proofs import publish
    observations,derived_ids=await publish(broker,state)
    readable.update(derived_ids)
    async def update(value):
        sync_usage(state)
        capture_access(value,state.evidence,current(),state.incident_id)
        broker.snapshot=value
        state.flags['attribution_v2']=value.model_dump(mode='json')
        if hasattr(broker.board,'set_attribution'):
            await broker.board.set_attribution(state.incident_id,state.flags['attribution_v2'])
    snapshot=await run(snapshot,tools=broker.definitions,fetch=broker.fetch,readable=readable,
        budget=state.budget,gateway=getattr(owner.router,'gateway',None),
        model=getattr(owner.router,'main_model',''),observations=observations,
        on_update=update,availability=broker.availability,
        on_plan=lambda value:setattr(broker,'snapshot',value))
    await update(snapshot)
    state.round=state.budget.rounds
    apply_verdict(state,snapshot)
    emit_step_phase(emitter,'done','hypotheses','按问题生成候选与验证',
        {'problems':len(snapshot.problems),'round':state.round},
        result=f'{len(snapshot.problems)} 个问题；'+snapshot.stop_reason)


def apply_verdict(state,snapshot):
    state.hypotheses=[]
    projection=project_root(snapshot)
    root=None
    if projection:
        p=snapshot.problems[0]
        c=next(c for c in p.candidates+snapshot.candidate_backlog.get(p.problem_id,[]) if c.status=='confirmed')
        root=Hypothesis(hypothesis_id=c.candidate_id,statement=p.cause.statement,
            category=c.category,root_entity=EntityRef.make('service',p.service,cluster=p.cluster),
            prior=0,score=c.sufficiency,
            supporting=p.cause.evidence_ids,status=HypothesisStatus.CONFIRMED)
        state.hypotheses=[root]
    state.arbitration=ArbitResult(decision=ArbitDecision.CONCLUDED if root else ArbitDecision.ABSTAIN,
        root_cause=root,reason=snapshot.stop_reason,
        report='按问题保留事实、机制、具体候选和验证缺口；'+snapshot.stop_reason)
    state.critic={'version':snapshot.score_version,'problem_count':len(snapshot.problems)}


def business_snapshot(problems,verification):
    return Snapshot(problems=[p for p in problems if not p.error_type.startswith('SDK_ACCESS_')],
        observability_problems=[p for p in problems if p.error_type.startswith('SDK_ACCESS_')],
        sdk_verification=verification,source={'sdk_separated':True})
