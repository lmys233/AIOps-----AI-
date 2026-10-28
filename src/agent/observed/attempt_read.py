"""Scoped durable originals are readable through the existing raw tool permission."""
from .context import current
from .attempts import store_of
from ..tools.sources import RawOutcome,QueryRejected
import json


async def raw(ref,params):
    scope=current();parts=ref.split(':',2)
    if not scope or len(parts)!=3 or parts[1]!=scope.observed_id:raise QueryRejected('调用引用不属于当前系统')
    row=await store_of(scope).detail(scope.tenant,scope.observed_id,parts[2]);service=row['body']['identity']['service']
    if service!='_system' and service not in await scope.registered():raise QueryRejected('调用记录服务未登记或已停用')
    text=json.dumps(row,ensure_ascii=False);limit=max(1,int(params.get('max_chars') or 20000))
    clipped=len(text)>limit
    return RawOutcome(data=text[:limit],raw_ref=ref,note='持久调用档案，原时间保留'+('；正文已截断，可从详情 API 读取完整结构' if clipped else '；完整'))
