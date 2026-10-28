"""Metadata-driven native series. Changes are observations, not confirmed causes."""
import hashlib
import json
import math
import re
import statistics
from .base import ToolRequest
from .query_labels import service_pattern
from .anomaly import Signal,detect_series

def groups(ctx):
    grouped={}
    for descriptor in ctx.metric_catalog or ():
        if not set(descriptor['services'])&set(ctx.services):continue
        if descriptor.get('origin') in ('observation_path','mixed'):continue
        if descriptor.get('type','unknown') not in ('gauge','counter','sampled_value','unknown'):continue
        # No raw counter trend, or invented histogram quantile, is interpreted as an anomaly.
        family=descriptor.get('family') or descriptor['name']
        signature=(family,descriptor.get('unit'),descriptor.get('type','unknown'),descriptor.get('statistic','unknown'))
        key='native_metric_'+hashlib.sha256(json.dumps(signature).encode()).hexdigest()[:12]
        grouped.setdefault(key,{'family':family,'unit':descriptor.get('unit'),'type':signature[2],
            'statistic':signature[3],'names':[]})['names'].append(descriptor['name'])
    return grouped

def requests(ctx,*,covered_names=()):
    result=[]
    for key,group in sorted(groups(ctx).items(),key=lambda item:(item[1]['family'],item[0])):
        names=sorted(set(group['names'])-set(covered_names))
        if not names:continue
        # Select exact original names, including names not valid as bare PromQL identifiers.
        selector='{__name__=~'+json.dumps('|'.join(re.escape(n) for n in names))+',service=~'+json.dumps(service_pattern(ctx.services))+'}'
        query=f'rate({selector}[1m])' if group['type']=='counter' else selector
        statistic='rate_1m' if group['type']=='counter' else group['statistic']
        result.append(ToolRequest(tool='promql_read',params={'query':query,**ctx.query_window(),'step':'30s','limit':10},
            signal=key,rationale=f'按目录读取原生指标 {group["family"]}；type={group["type"]}，unit={group["unit"] or "未知"}，statistic={statistic}；只描述变化，不确认故障原因'))
    if len(result)>8:
        from dataclasses import replace
        extra=len(result)-8;result=result[:8]
        result[-1]=replace(result[-1],dropped=f'{extra} 组原生指标超过本批8组上限，未查询；指标目录存在不表示已取证')
    return result

def evidence(agent,ctx,request,result):
    group=groups(ctx).get(request.signal)
    if not group:return []
    from .anomaly import series_of,point_time
    from ..common.enums import EvidenceDirection
    out=[]
    for entry in series_of(getattr(result,'data',None)):
        labels=entry.get('metric') or {};service=str(labels.get('service') or '')
        if service not in ctx.services:continue
        points=[p for p in entry.get('points',[]) if len(p)>=2 and isinstance(p[1],(int,float)) and math.isfinite(p[1])]
        if not points:continue
        raw_name=labels.get('__name__') or group['family'];unit=group['unit'] or '原始单位（未声明）'
        if group['type']=='counter':unit=(group['unit']+'/s') if group['unit'] else '原始计数单位/秒（计数单位未声明）'
        statistic='rate_1m' if group['type']=='counter' else group['statistic']
        if agent.snapshot or group['type']=='unknown':
            out.append(agent.make_evidence(ctx,signal='native_metric_snapshot',direction=EvidenceDirection.FLAT,
                onset=point_time(points[-1]),baseline=float(points[0][1]),magnitude=float(points[-1][1]),magnitude_unit=unit,
                entity=ctx.entity(service),confidence=.6,raw_ref=result.raw_ref+' | series='+json.dumps(labels,sort_keys=True),
                summary=f'{service} 的 {raw_name} 读数 {points[-1][1]:.5g}；type={group["type"]}，unit={unit}，statistic={statistic}；未据此判断故障',
                tags=['metrics','native_metric',f'metric_name={raw_name}',f'sample_count={len(points)}',
                      f'metric_type={group["type"]}',f'statistic={statistic}']))
            continue
        if len(points)<3:continue
        baseline=[float(p[1]) for p in points[:max(3,len(points)//4)]]
        center=statistics.median(baseline);mad=statistics.median(abs(x-center) for x in baseline)
        floor=max(6*mad,abs(center)*.5,max(abs(float(p[1])) for p in points)*1e-12,1e-15)
        signal=Signal(request.signal,unit,min_abs=floor,ratio=.5)
        detected=detect_series(dict(entry,points=points),signal)
        if detected is None:continue
        out.append(agent.make_evidence(ctx,signal=request.signal,direction=detected.direction,
            onset=detected.onset,baseline=detected.baseline,magnitude=detected.magnitude,magnitude_unit=unit,
            entity=ctx.entity(service),confidence=.6,raw_ref=result.raw_ref,
            summary=f'{service} 原生指标 {raw_name} 相对基线发生变化：起点 {detected.baseline:.5g}，峰值 {detected.peak:.5g}，变化 {detected.magnitude:.5g}；unit={unit}，statistic={statistic}；属于采样变化，故障与形成原因待核查',
            tags=['metrics','native_metric',f'metric_name={raw_name}',
                'metric_metadata='+json.dumps({k:v for k,v in group.items() if k!='names'},ensure_ascii=False)]))
    return out
