"""Publish computed samples without a second model inventing health conclusions."""
from datetime import datetime,timezone

LIMIT='这些是返回样本的统计，不是整个窗口的精确时间加权平均；未返回数据不等于0。仅凭这些读数不能证明服务正常、无故障或已恢复。'


def render(result,window):
    details=[];summaries=[]
    if result.get('missing_metrics'):
        details.append({'label':'未返回的指定指标','value':'、'.join(result['missing_metrics'])+'；没有用其他指标替代，也不按0计算。'})
    for item in result.get('computations',[]):
        series=item.get('series') or {};data=item.get('data') or {}
        name=' · '.join(str(series.get(k) or '') for k in ('service','metric')).strip(' ·') or item['tool']
        unit=series.get('unit') or '单位未声明'
        if not item.get('ok'):
            details.append({'label':name,'value':'计算未完成：'+str(item.get('summary') or '')});continue
        if item['tool']=='series_stats':
            value=f"最新值 {data['last']} {unit}；样本峰值 {data['max']} {unit}；样本均值 {data['mean']} {unit}；样本数 {data['count']}"
        else:value=str(item.get('summary') or '')
        summaries.append(name+'：'+value)
        details.append({'label':name,'value':value})
        if data.get('last_ts') is not None:
            details.append({'label':name+' · 末次采样','value':datetime.fromtimestamp(data['last_ts'],timezone.utc).isoformat()})
    details.extend([{'label':'查询窗口','value':str(window or '见各查询记录')},{'label':'覆盖限制','value':LIMIT}])
    if len(summaries)>2:details.append({'label':'展示范围','value':'摘要列前两项，各序列完整计算结果见明细。'})
    return {'source':'metric_tools','summary':('；'.join(summaries[:2]) if summaries else '没有取得可用的计算结果')+'。'+LIMIT,
            'details':details,'details_kind':'table','note':'直接展示工具计算和序列身份，不追加健康判断',
            'metric_result':result}
