"""The same active-observation details for chat, history and Markdown."""
from copy import deepcopy
import json


def apply(body,activity):
    if not activity:return
    ex=body['explanation'];ex['active_probe']=deepcopy(activity)
    answer=ex['answer'];rows=answer.setdefault('details',[])
    if activity.get('action')=='ask':
        question=activity['question']
        # Keep already found anomalies in the conclusion; never suppress their diagnosis.
        found=bool((ex.get('attribution_v2') or {}).get('problems'))
        snapshot=ex.get('attribution_v2') or {}
        if snapshot.get('source',{}).get('sdk_separated'):
            from ..attribution.v2.presentation import observability_summary
            chain=observability_summary(snapshot)
        else:chain=''
        prefix=answer.get('summary','')+' ' if found else ('本窗口采样未见可定位的业务故障。' if not body.get('degraded_signals') else '本次部分数据未查成，无法判断完整状况。')+chain+' '
        answer.update(source='active_probe',summary=prefix+question,note='保持原任务；等待接口/参数/单次写请求确认，没有因反问自动发流量')
        body['clarification']={'questions':[dict(question=question,choices=[],plugin_id='active_probe',slot='endpoint')],
            'reason':activity.get('reason',''),'plugin_id':'active_probe'}
        body['status']='clarify'
        body['root_cause']=None
    elif activity.get('runs'):
        summary=' '.join(r['result'].get('summary','') for r in activity['runs'])
        head=answer.get('summary','')
        if activity.get('observations'):head=head.replace('窗口内发现','历史与本轮主动核验样本中发现')
        answer['summary']=head+' 主动核验：'+summary
    if activity.get('reason'):rows.append(dict(label='主动核验决策',value=activity['reason']))
    for e in activity.get('candidate_endpoints',[]):
        rows.append(dict(label='候选接口 · '+e['service']+' · '+e['method']+' '+e['path'],
            value=e['description']+'；副作用 '+e['effect']+'；实例 '+e['instance']+'；编号 '+e['endpoint_id']))
    body.setdefault('budget',{}).setdefault('enforced',{}).update(active_http_registered_scope=True,
        active_http_limits=True,active_http_write_confirmation=True)
    for i,run in enumerate(activity.get('runs',[]),1):
        rows.append(dict(label=f'主动核验 {i} · 完整请求参数',value=json.dumps(run['params'],ensure_ascii=False)))
        rows.append(dict(label=f'主动核验 {i} · 实际结果',value=run['result'].get('summary','')))
        sdk=run.get('sdk_observation') or {}
        if sdk:rows.append(dict(label=f'主动核验 {i} · SDK关联',value=sdk.get('summary','')))
        for key in ('metric_before','metric_after'):
            metric=run.get(key)
            if metric:rows.append(dict(label=f'主动核验 {i} · '+key,value=metric['result'].get('summary','')+'；'+metric['note']))
    if activity.get('pending'):
        rows.append(dict(label='待确认业务写请求 · 完整方案',value=json.dumps(activity['pending'],ensure_ascii=False)))
    rows.append(dict(label='主动样本口径',value='web_query产生合成流量；本次完成率与观测窗口不能证明全量正常、生产故障或系统容量。业务写请求可能已生效，不自动重发。'))
    body['report']=answer['summary']
