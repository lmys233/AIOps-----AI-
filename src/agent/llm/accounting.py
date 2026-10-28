"""Reported usage and conservative reservations are separate, never billing estimates."""
import dataclasses
import json
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from uuid import uuid4
from contextlib import contextmanager
from ..common.tokens import estimate_tokens

CALL_RECORDS=ContextVar('model_call_records',default=None)
REQUEST_RECORDS=ContextVar('model_request_records',default=None)


@contextmanager
def collect_records():
    """Collect one operation locally; forward once to its enclosing collector."""
    parent=CALL_RECORDS.get();records=[];token=CALL_RECORDS.set(records)
    try:yield records
    finally:
        CALL_RECORDS.reset(token)
        if parent is not None:
            parent.extend(r for r in records if not any(old is r for old in parent))


def attempt_receipt(records,error=None,*,recording_supported=False):
    """Expose recorded dispatches, not configured caps or concurrent request slices."""
    records=list(records)
    count=sum(r.attempts for r in records) if records or recording_supported else None
    if count is None and error is not None and getattr(error,'attempts',0)>0:count=error.attempts
    kind=getattr(error,'failure_kind','') or (records[-1].extra.get('failure_kind','') if error and records else '') or ('cancelled' if type(error).__name__=='CancelledError' else
        'model_plan_failed' if error else '')
    reasons={'stage_deadline':'模型阶段总期限已到，停止重试；没有重启整段规划',
        'attempt_limit':'已达到本逻辑调用的物理尝试上限',
        'task_budget':'累计模型、Token或耗时预算不足，停止新增请求',
        'context_budget':'受保护上下文超过单次上限，未派发模型请求',
        'permission_denied':'上游拒绝权限，不能通过重试绕过',
        'http_nonretryable':'上游返回不可重试错误',
        'retry_after_exceeds_policy':'上游要求的等待超出本阶段允许范围，没有提前重试',
        'cancelled':'调用已取消，未自动重发',
        'transport_error':'供应商连接失败，本逻辑调用已停止',
        'output_truncated':'输出截断且已用完本类重试额度',
        'model_plan_failed':'未取得有效规划结果，未启动额外的外层重试'}
    stop=reasons.get(kind,'该失败已停止，未启动额外的外层重试') if error else (
        '本轮未新发模型请求，使用已完成结果' if count==0 else '已取得有效结果，无需继续重试')
    fields=('number','status','failure_kind','http_status','finish_reason','retry_reason',
        'retry_backoff_sec','started_at','finished_at','usage_known')
    attempts=[{'model_call_id':r.call_id,**{k:a[k] for k in fields if k in a}}
        for r in records for a in r.extra.get('attempt_records',[])]
    retries=sum(max(0,r.attempts-1) for r in records) if records else max(0,count-1) if count is not None else None
    return {'physical_attempts':count,'retry_count':retries,
        'attempt_records':attempts,'failure_kind':kind,'retry_stop_reason':stop,
        'retry_policy':records[-1].extra.get('retry_policy',{}) if records else {}}


def summary(records):
    records=list(records)
    return {'input_tokens':sum(r.input_tokens for r in records),
            'output_tokens':sum(r.output_tokens for r in records),
            'reported_tokens':sum(r.input_tokens+r.output_tokens for r in records),
            'logical_calls':len(records),'physical_attempts':sum(max(0,r.attempts) for r in records),
            'reserved_tokens':sum(max(0,getattr(r,'reserved_tokens',0)) for r in records),
            'usage_known':all(getattr(r,'usage_known',False) for r in records),
            'unknown_calls':sum(not getattr(r,'usage_known',False) for r in records),
            'scope':'供应商已报告用量；预留为预算估算，不是账单'}


def record_data(record):return dataclasses.asdict(record)


def usage(data):
    values=data.get('usage') if isinstance(data,dict) else None
    if not isinstance(values,dict):return 0,0,False
    a,b=values.get('prompt_tokens'),values.get('completion_tokens')
    valid=lambda v:isinstance(v,int) and not isinstance(v,bool) and v>=0
    return a if valid(a) else 0,b if valid(b) else 0,valid(a) and valid(b)


class CallMeter:
    def __init__(self):
        from ..execution.runtime import MODEL_STEP
        step=MODEL_STEP.get()
        self.call_id=step[1]['call_id'] if step else 'mdl_'+uuid4().hex
        self.rows=[];self.started=time.perf_counter()

    async def begin(self,body,context_limit=None):
        from ..common.model_budget import guard_attempt
        ticket=guard_attempt(body)
        amount=estimate_tokens(json.dumps({k:v for k,v in body.items() if k!='max_tokens'},ensure_ascii=False))+body['max_tokens']
        row={'number':len(self.rows)+1,'status':'reserved','usage_known':False,
            'reservation_tokens':amount,'reserved_tokens':amount,'input_tokens':0,'output_tokens':0,
            'started_at':datetime.now(timezone.utc).isoformat()}
        from .context import occupancy
        if context_limit is not None:row['context']=occupancy(body,context_limit)
        from ..execution.runtime import model_attempt_event
        await model_attempt_event(row)
        self.rows.append(row)
        return row,ticket

    async def finish(self,row,ticket,status,data=None):
        from ..common.model_budget import settle_attempt
        a,b,known=usage(data)
        row.update(status=status,input_tokens=a,output_tokens=b,usage_known=known,
            reserved_tokens=0 if known else max(0,row['reservation_tokens']-a-b),
            finished_at=datetime.now(timezone.utc).isoformat())
        settle_attempt(ticket,a+b,known)
        from ..execution.runtime import model_attempt_event
        await model_attempt_event(row)

    def totals(self):
        return {'input_tokens':sum(r['input_tokens'] for r in self.rows),
            'output_tokens':sum(r['output_tokens'] for r in self.rows),
            'usage_known':all(r['usage_known'] for r in self.rows),
            'reserved_tokens':sum(r['reserved_tokens'] for r in self.rows)}
