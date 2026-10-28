"""优先安排可执行的区分性只读任务，同一查询只发一次。"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Any
from .contracts import Snapshot
from .facts import stable_id
from ...tools.availability import Availability, safe_detail


@dataclass
class Query:
    query_id: str
    tool: str
    params: dict[str, Any]
    requirements: list[tuple[str,str,str]] = field(default_factory=list)
    priority: float = 0
    rationale: str = ''
    query_spec: Any = None


def safe_params(req, p, schema: dict) -> dict | None:
    if req.tool=='db_object_read' and any(re.search(r'\bcolumn\s+["\']',s,re.I) for s in p.samples):
        return None  # A relation catalog cannot validate a missing column without table context.
    props = schema.get('properties', {})
    params = {k:v for k,v in req.params.items() if k in props}
    for key in ('service','object_name','env','cluster','instance','request_id'):
        if key in props:
            expected = getattr(p,key)
            if key in ('cluster','instance','request_id') and not expected:
                params.pop(key,None);continue
            if key in params and params[key] != expected:
                return None
            params[key] = expected
    if p.time_range[0] < p.time_range[1]:
        for key,value in [('start',p.time_range[0].isoformat()),('end',p.time_range[1].isoformat())]:
            if key in props:params[key]=value
    elif {'start','end'} & set(props):
        # 单点事实不能充当范围查询；命名检查允许无时间参数，返回独立覆盖信息。
        if 'query' in props or {'start','end'} & set(schema.get('required',[])):return None
        params.pop('start',None);params.pop('end',None)
    if 'query' in params:
        names=re.findall(r'service\s*=~?\s*["\']([^"\']+)',str(params['query']))
        if not names or any(n != p.service for n in names):return None
    if any(k not in params or params[k] in ('',None) for k in schema.get('required', [])):
        return None
    return params


def plan(snapshot: Snapshot, tools: dict[str,dict], visited: set[str], limit=8, availability=None) -> list[Query]:
    tasks: dict[str,Query] = {}
    checked={}
    for p in snapshot.problems:
        active=[c for c in p.candidates if c.status=='candidate']
        for c in active:
            for r in c.requirements:
                if r.state != 'not_queried':continue
                if not r.tool or r.tool not in tools:
                    r.reason='能力缺口：未接入 ' + (r.tool or r.signal)
                    p.gaps.append(r.question+'：'+r.reason);continue
                params=safe_params(r,p,tools[r.tool])
                if params is None:
                    r.reason='查询参数缺失或超出问题作用域';p.gaps.append(r.question+'：'+r.reason);continue
                qid=stable_id('Q',p.env,p.cluster,r.tool,str(sorted(params.items())))
                if qid in visited:continue
                if qid not in checked:
                    checked[qid]=availability(r.tool,params) if availability else Availability()
                ready=checked[qid]
                if not ready.ready:
                    record_gap(snapshot,p,r,ready);continue
                discriminating=1/sum(any(x.signal==r.signal for x in a.requirements) for a in active)
                q=tasks.setdefault(qid,Query(qid,r.tool,params,rationale=r.question))
                q.requirements.append((p.problem_id,c.candidate_id,r.key))
                if r.question not in q.rationale:q.rationale+='；'+r.question
                q.priority=max(q.priority,discriminating + (1 if r.required else 0))
    # 每个服务先取一项，再按区分性补充，避免热服务吃光单轮额度。
    ordered=sorted(tasks.values(),key=lambda q:(-q.priority,q.query_id))
    first,rest,seen=[],[],set()
    for q in ordered:
        name=q.params.get('service') or q.requirements[0][0]
        (rest if name in seen else first).append(q);seen.add(name)
    return (first+rest)[:limit]


def record_gap(snapshot,p,r,ready):
    reason='能力缺口：'+ready.reason+'；接入方式：'+ready.remedy
    r.reason=safe_detail(reason)
    gap={'tool':r.tool,'service':safe_detail(p.service),'object_name':safe_detail(p.object_name),
         'status':ready.status,'reason':ready.reason,'remedy':ready.remedy}
    if gap not in snapshot.capability_gaps:snapshot.capability_gaps.append(gap)
    text=r.question+'：'+r.reason
    if text not in p.gaps:p.gaps.append(text)
