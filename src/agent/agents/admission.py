"""Admit bounded reads before dispatch; keep capacity for followups and verification."""
from ..common.tool_budget import CURRENT

def admit(plan):
    scope=CURRENT.get()
    if scope is None:return
    budget=scope[0];remaining=max(0,budget.max_tool_calls-budget.tool_calls)
    reserve=min(12,max(1,remaining//4)) if remaining>2 else 0
    allowance=max(0,remaining-reserve)
    if len(plan.calls)<=allowance:return
    by_role={}
    for index,call in enumerate(plan.calls):by_role.setdefault(call.role,[]).append((index,call))
    for rows in by_role.values():rows.sort(key=lambda row:row[1].purpose!='evidence')
    picked=[]
    # At least one evidence read per available source, then broad metrics/log coverage.
    for role,rows in by_role.items():
        if rows and len(picked)<allowance:picked.append(rows.pop(0))
    for role in ('metrics_agent','log_agent'):
        rows=by_role.get(role,[])
        while rows and len(picked)<allowance:picked.append(rows.pop(0))
    while len(picked)<allowance and any(by_role.values()):
        for rows in by_role.values():
            if rows and len(picked)<allowance:picked.append(rows.pop(0))
    keep={i for i,c in picked}
    deferred=[c for i,c in enumerate(plan.calls) if i not in keep]
    plan.deferred.extend(deferred)
    plan.calls=[c for i,c in sorted(picked)]
    for role in sorted({c.role for c in deferred}):
        calls=[c for c in deferred if c.role==role]
        names=sorted({n for c in calls for n in c.batch_services})
        plan.degraded.append((role+'_budget_unqueried',
            f'预算未查：{role} 的 {len(calls)} 项查询未派发；服务 {"、".join(names)}；本轮保留 {reserve} 次工具额度供辅助/定向核查'))
    plan.degraded_sources=list(dict.fromkeys([*plan.degraded_sources,
        *(c.evidence_source for c in deferred if c.evidence_source)]))
