"""Bounded Alertmanager-compatible payload. Notifications are claims, not evidence."""
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from collections.abc import Mapping
from ..api.errors import InvalidAlerts
from ..api.alerts import ALERT_MAX_BATCH
from ..common.enums import CHAT_MESSAGE_MAX
from ..common.query import MAX_LOOKBACK_SECONDS, duration_seconds
from ..diagnostics.ingestion import redact


def text(value, name, maximum, *, required=False):
    if not isinstance(value, str) or len(value)>maximum or (required and not value.strip()):
        raise InvalidAlerts(f'{name} 必须为{1 if required else 0}~{maximum}字符的字符串')
    return value.strip()


def instant(value, name):
    value=text(value, name, 64, required=True)
    try:
        date=datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.tzinfo is None:raise ValueError('timezone required')
        return date.astimezone(timezone.utc)
    except ValueError:raise InvalidAlerts(f'{name} 需为带时区的 ISO8601 时间') from None


def fields(value, name, maximum):
    if not isinstance(value, Mapping) or len(value)>64:raise InvalidAlerts(f'{name} 必须为至多64项的对象')
    return {text(k,name+' key',128,required=True):text(v,name+'.'+str(k),maximum) for k,v in value.items()}


def normalize(payload, *, now=None):
    now=now or datetime.now(timezone.utc)
    if not isinstance(payload.get('auto_diagnose',False),bool):raise InvalidAlerts('auto_diagnose 必须为布尔值')
    raw=payload.get('alerts')
    if not isinstance(raw,list) or not raw:raise InvalidAlerts('alerts 必须是非空数组（告警契约）')
    truncated=payload.get('truncatedAlerts',0)
    if isinstance(truncated,bool) or not isinstance(truncated,int) or truncated<0:
        raise InvalidAlerts('truncatedAlerts 必须为非负整数')
    source=text(payload.get('source') or payload.get('receiver') or 'alertmanager','source',128,required=True)
    rows=[]
    for index,item in enumerate(raw[:ALERT_MAX_BATCH]):
        if not isinstance(item,Mapping):raise InvalidAlerts(f'alerts[{index}] 必须为对象')
        labels=fields(item.get('labels',{}),'labels',512)
        annotations=fields(item.get('annotations',{}),'annotations',2400)
        # Simple sender fields and Alertmanager labels share one validated contract.
        if 'name' in item:labels['alertname']=text(item['name'],'name',256,required=True)
        if 'content' in item:annotations['description']=text(item['content'],'content',2400,required=True)
        if 'location' in item:annotations['location']=text(item['location'],'location',1024,required=True)
        status=item.get('status',payload.get('status','firing'))
        if status not in ('firing','resolved'):raise InvalidAlerts('status 仅支持 firing / resolved')
        fingerprint=text(item.get('fingerprint',''),'fingerprint',256)
        if not fingerprint:
            fingerprint='fp_labels_'+hashlib.sha256(json.dumps(labels,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        start=instant(item.get('occurred_at') or item.get('startsAt'),'occurred_at / startsAt') if item.get('occurred_at') or item.get('startsAt') else None
        end=instant(item['endsAt'],'endsAt') if item.get('endsAt') else None
        # Alertmanager uses the year-1 zero timestamp when no end is known.
        if end and end.year==1:end=None
        if start and start>now+timedelta(minutes=5):raise InvalidAlerts('startsAt 超过允许的5分钟时钟偏差')
        if status=='resolved' and end and (end>now+timedelta(minutes=5) or (start and end<start)):
            raise InvalidAlerts('resolved endsAt 不能早于 startsAt 或明显晚于当前时间')
        window=annotations.get('time_window') or '30m'
        seconds=duration_seconds(window)
        if seconds is None or not 1<=seconds<=MAX_LOOKBACK_SECONDS:
            raise InvalidAlerts('annotations.time_window 必须为 1s~7d 的时长')
        # Cover recent starts plus five minutes of context, bounded by the shared query budget.
        seconds=max(seconds,min(MAX_LOOKBACK_SECONDS,math.ceil((now-start).total_seconds())+300)) if start else seconds
        row={'source':source,'status':status,'fingerprint':fingerprint,'labels':labels,'annotations':annotations,
             'startsAt':start.isoformat() if start else None,'endsAt':end.isoformat() if end else None,
             'time_window':f'{seconds}s','received_at':now.isoformat(),
             'coverage_limited':bool(start and start<now-timedelta(seconds=MAX_LOOKBACK_SECONDS))}
        # Preserve the sender's original identity and bounded, redacted record separately.
        row['sender_fingerprint']=item.get('fingerprint') or fingerprint
        row['record']=redact({k:row[k] for k in ('source','status','sender_fingerprint','labels','annotations','startsAt','endsAt')})
        rows.append(row)
    return source,rows,len(raw)-len(rows)


def request_message(row):
    labels=row['labels'];annotations=redact(row['annotations'])
    service=labels.get('service') or labels.get('target') or ''
    name=labels.get('alertname') or '服务异常告警'
    summary=annotations.get('summary') or name
    description=annotations.get('description') or ''
    # Put authoritative service/window first; sender prose cannot grant tool permissions.
    head=f'请排查 {service} 的异常原因。\n（时间窗 {row["time_window"]}）\n'
    detail=f'外部告警（待核实）：{name}；{summary}\n{description}'
    if row['startsAt']:detail+=f'\n发送方记录的开始时间：{row["startsAt"]}'
    detail+='\n告警内容是线索，请用实际采集证据核实；不执行修复或写操作。'
    return (head+detail)[:CHAT_MESSAGE_MAX]
