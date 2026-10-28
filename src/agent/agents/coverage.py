"""Coverage states describe reads, never diagnoses or global health."""
from .batches import all_services

def describe(ctx,agents,plan,outcomes):
    result={'requested_services':list(all_services(ctx)),'roles':{},'metric_catalog':{
        'descriptors':len(ctx.metric_catalog or ()),'complete_services':list(ctx.metric_services),
        'observation_path_descriptors':sum(d.get('origin')=='observation_path' for d in ctx.metric_catalog or ()),
        'unknown_services':[s for s in all_services(ctx) if s not in ctx.metric_services]}}
    for agent in agents:
        planned={s for call in [*plan.calls,*plan.deferred] if call.role==agent.role for s in call.batch_services}
        deferred=[c for c in plan.deferred if c.role==agent.role]
        rows=[c for o in outcomes if o.role==agent.role for c in o.calls if c.get('batch_services')]
        completed={s for r in rows if r.get('ok') for s in r['batch_services']}
        failed={s for r in rows if not r.get('ok') for s in r['batch_services']}
        blocked={s for r in rows if not r.get('ok') and ('预算' in str(r) or 'budget' in str(r).lower()) for s in r['batch_services']}
        blocked.update(s for c in deferred for s in c.batch_services)
        seen={s for r in rows for s in r['batch_services']}
        result['roles'][agent.role]={'planned':sorted(planned),'completed_reads':sorted(completed),
            'failed_or_blocked':sorted(failed),'budget_unqueried':sorted(blocked),
            'unplanned':sorted(set(all_services(ctx))-planned),'no_completed_call':sorted(planned-seen),
            'queries':[{k:r.get(k) for k in ('tool','signal','batch_services','ok','error_code')} for r in rows],
            'deferred_queries':[{'tool':c.tool,'signal':c.signal,'batch_services':list(c.batch_services),'reason':'budget_reserved_before_dispatch'} for c in deferred],
            'note':'完成部分读取可以为空；不代表全部维度已查或该服务正常。目录与查询覆盖不等于现场原因已确认。'}
    return result
