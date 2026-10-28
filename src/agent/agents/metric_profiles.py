"""Rank investigation targets from existing samples; never certify causality.

The frozen robust-shift-v1 rule uses no service names, metric families, labels or
injection timestamps. Units are retained; standardized scores are priorities,
not probabilities or saturation/availability claims.
"""
import math,statistics
from .batches import all_services

VERSION='robust-shift-v1'


def profile(points):
    rows=sorted((float(row[0]),float(row[1])) for row in points
        if isinstance(row,(tuple,list)) and len(row)>=2 and
        type(row[0]) in (int,float) and type(row[1]) in (int,float) and math.isfinite(row[0]) and math.isfinite(row[1]))
    if len(rows)<6:return None
    n=max(3,len(rows)//4);baseline=[v for _,v in rows[:n]];recent=[v for _,v in rows[-n:]]
    center=statistics.median(baseline);last=statistics.median(recent)
    if not all(math.isfinite(x) for x in (center,last)):return None
    mad=statistics.median(abs(v-center) for v in baseline)
    spread=max(baseline)-min(baseline)
    scale=max(1.4826*mad,spread/2,abs(center)*.05,max(abs(v) for _,v in rows)*1e-12,1e-300)
    if not math.isfinite(scale) or scale<=0:return None
    effect=min(abs(last-center)/scale,1e6)
    direction=1 if last>=center else -1
    persistence=sum(direction*(v-center)>3*scale for _,v in rows[n:])/len(rows[n:])
    score=math.log1p(min(effect,1e6))*persistence if effect>3 else 0.
    onset=next((t for t,v in rows[n:] if direction*(v-center)>3*scale),None)
    return dict(samples=len(rows),start=rows[0][0],end=rows[-1][0],baseline=center,recent=last,
        standardized_change=round(effect,6),persistence=round(persistence,6),
        priority_score=round(score,6),onset=onset,
        direction='up' if last>center else 'down' if last<center else 'flat')


def describe(ctx,outcomes):
    metadata={(d['name'],s):d for d in ctx.metric_catalog or () for s in d['services']}
    profiles=[];seen=set()
    for outcome in outcomes:
        if outcome.role!='metrics_agent':continue
        for signal,result in outcome.results.items():
            if not result.ok or not signal.startswith('native_metric_') or not isinstance(result.data,dict):continue
            for row in result.data.get('series',[]):
                labels=row.get('metric') or {};name=labels.get('__name__');service=labels.get('service')
                descriptor=metadata.get((name,service))
                if not descriptor or descriptor.get('origin') in ('observation_path','mixed'):continue
                if descriptor.get('type') not in ('gauge','sampled_value'):continue
                identity=(name,service,str(sorted(labels.items())),result.raw_ref)
                if identity in seen:continue
                seen.add(identity);measured=profile(row.get('points') or [])
                if measured is None:continue
                profiles.append(dict(service=service,metric=name,type=descriptor['type'],
                    unit=descriptor.get('unit'),statistic=descriptor.get('statistic','unknown'),
                    evidence_ids=[e.evidence_id for e in getattr(outcome,'evidence',()) if e.entity.name==service and
                        e.raw_ref==result.raw_ref and 'metric_name='+name in e.tags],
                    raw_ref=result.raw_ref,**measured))
    profiles.sort(key=lambda row:(-row['priority_score'],row['service'],row['metric']))
    total=len(profiles);profiles=profiles[:256];ranked={}
    for row in profiles:
        if row['priority_score']<=0:continue
        item=ranked.setdefault(row['service'],dict(service=row['service'],priority_score=row['priority_score'],
            metrics=[],raw_refs=[],evidence_ids=[],confirmed=False))
        if len(item['metrics'])<3:item['metrics'].append(row['metric'])
        if row['raw_ref'] not in item['raw_refs'] and len(item['raw_refs'])<3:item['raw_refs'].append(row['raw_ref'])
        item['evidence_ids']=list(dict.fromkeys(item['evidence_ids']+row['evidence_ids']))[:6]
    return dict(version=VERSION,kind='investigation_priority',causal_confirmation=False,
        ranked_services=list(ranked.values())[:10],profiles=profiles,total_profiles=total,
        profiles_truncated=total>256,unknown_services=[s for s in all_services(ctx) if s not in {r['service'] for r in profiles}],
        note='按本轮有类型的指标样本变化强度安排核查顺序；不是根因确认，不是概率，也不证明资源耗尽。单位缺失保持未知；空结果不能证明正常。')
