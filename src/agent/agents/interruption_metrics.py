"""Explicit interruption metric requests; SDK counter absence is not zero."""
from .anomaly import Signal
from .base import ToolRequest
from datetime import timedelta

SIGNALS={name:Signal(name,unit) for name,unit in (
    ('request_cancellation_rate','%'),('request_timeout_rate','%'),
    ('request_cancellation_count','次'),('request_timeout_count','次'))}


def calls(ctx,pattern,step):
    text=(ctx.slots.symptom or '').lower()
    cancel=any(s in text for s in ('取消率','取消次数','cancellation rate','cancel count'))
    timeout=any(s in text for s in ('超时率','超时次数','timeout rate','timeout count'))
    if not cancel and not timeout:return None
    count=any(s in text for s in ('次数','count'));outcome='cancelled' if cancel else 'timeout'
    name=('request_cancellation' if cancel else 'request_timeout')+('_count' if count else '_rate')
    duration=f'{max(1,int(ctx.window_seconds))}s' if count else '1m'
    base='aiops_http_request_seconds_count{service=~"'+pattern+'"'
    total=f'sum by (service) ({"increase" if count else "rate"}({base+"}"}[{duration}]))'
    chosen=f'sum by (service) ({"increase" if count else "rate"}({base},outcome="{outcome}"}}[{duration}]))'
    # Only create zero when the SDK's total counter exists for that service.
    numerator=f'({chosen} or on(service) (0 * {total}))'
    query=numerator if count else f'100 * {numerator} / {total}'
    start=(ctx.window_end-timedelta(seconds=step*3)).isoformat()
    return [ToolRequest(tool='promql_read',signal=name,params=dict(query=query,start=start,
        end=ctx.window_end.isoformat(),step=f'{step}s',limit=10),
        rationale='只读取实际 SDK 请求计数；无 SDK 序列或分母为零时不能给出取消/超时比例')]
