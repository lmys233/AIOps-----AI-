"""仅明确历史追问或真实方案选择复用快照；当前状态仍重新取证。"""
from ...common.enums import Intent
from ...remediation.plans import _plan_from_entry
from .contracts import Snapshot
from .runtime import apply_verdict


def investigation_snapshot(body,incident_id,stamp,age,evidence,services):
    explanation=body.get('explanation') or {}
    return {'incident_id':incident_id,'at':stamp,'age_sec':round(age,1),
        'evidence':evidence,'services':services,
        'hypotheses':[item for item in body.get('hypotheses') or [] if isinstance(item,dict)],
        'attribution_v2':explanation.get('attribution_v2'),
        'remediation_plans':explanation.get('remediation_plans') or []}


def reuse(state):
    if state.intent!=Intent.REMEDIATION:return False
    text=state.user_message
    selected=state.flags.get('selected_plan')
    explicit=any(w in text for w in ('刚才','上一轮','刚刚','刚查到'))
    if (not explicit and selected is None) or any(w in text for w in ('现在','当前还有','重新查')):
        return False
    carried=state.flags.get('inherited_investigation') or {}
    if not carried.get('attribution_v2'):return False
    snapshot=Snapshot.model_validate(carried['attribution_v2'])
    names=set(state.slots.service_names or [])
    if state.slots.service:names.add(state.slots.service.name)
    if names:
        snapshot.problems=[p for p in snapshot.problems if p.service in names]
        snapshot.sdk_access_checks=[c for c in snapshot.sdk_access_checks if c.identity.get('service') in names]
        snapshot.observability_problems=[p for p in snapshot.observability_problems if p.service in names]
        if snapshot.sdk_verification:snapshot.sdk_verification['checks']=[c for c in snapshot.sdk_verification.get('checks',[]) if c['service'] in names]
    if not snapshot.problems:return False
    snapshot.source=dict(snapshot.source,kind='previous_snapshot',incident_id=carried['incident_id'],at=carried['at'])
    state.flags['attribution_v2']=snapshot.model_dump(mode='json')
    if selected is None and carried.get('remediation_plans') and not names:
        state.flags['remediation_plans']=[_plan_from_entry(p) for p in carried['remediation_plans']]
    apply_verdict(state,snapshot)
    state.status='concluded' if state.arbitration.root_cause else 'abstained'
    state.flags.pop('clarify_deferred',None)
    state.add_note('明确历史追问：复用事故 '+carried['incident_id']+' 的诊断快照，未冒充当前取证')
    return True
