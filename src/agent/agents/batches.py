"""Request-local service batches; per-query limits remain unchanged."""
from dataclasses import replace
import re
from .base import MAX_TARGET_SERVICES

def all_services(ctx):
    values=list(ctx.services)
    if ctx.slots.service is not None:values.append(ctx.slots.service.name)
    return tuple(dict.fromkeys(v for v in values if v))

def contexts(ctx,limit=MAX_TARGET_SERVICES):
    names=all_services(ctx)
    if not names:return [ctx]
    groups=[];group=[]
    for name in names:
        if group and (len(group)>=limit or len('|'.join(re.escape(n) for n in [*group,name]))>240):
            groups.append(group);group=[]
        group.append(name)
    if group:groups.append(group)
    result=[]
    for group in groups:
        entity=ctx.slots.service
        slots=ctx.slots.model_copy(update={'service_names':list(group),
            'service':entity if entity is not None and entity.name in group else None})
        scope=ctx.query_scope.model_copy(update={'services':tuple(group)}) if ctx.query_scope else None
        result.append(replace(ctx,slots=slots,query_scope=scope))
    return result
