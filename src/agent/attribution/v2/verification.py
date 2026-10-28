"""按独立、可追溯的命名只读事实验证；缺口不作为反证。"""
from __future__ import annotations
from datetime import datetime
from typing import Any
from pydantic import Field, model_validator
from .contracts import Candidate, Conclusion, Problem, Record, Requirement, Snapshot
from .seeds import SPECS


class Observation(Record):
    evidence_id: str = ''
    signal: str
    request_id: str = ''
    service: str
    object_name: str = ''
    env: str = ''
    cluster: str = ''
    endpoint: str = ''
    instance: str = ''
    boot_id: str = ''
    query_fingerprint: str = ''
    value: Any = None
    time_range: tuple[datetime, datetime]
    event_time: datetime | None = None
    complete: bool = False
    scope_verified: bool = False
    failed: bool = False
    mechanism_keys: list[str] = Field(default_factory=list)
    reason: str = ''

    @model_validator(mode='after')
    def valid_time(self):
        if any(t.tzinfo is None for t in self.time_range) or self.time_range[1]<self.time_range[0]:
            raise ValueError('检查证据必须标明时区和有效覆盖区间')
        if self.event_time is not None and self.event_time.tzinfo is None:
            raise ValueError('变更事件必须标明时区')
        return self


def compare(value: Any, requirement: Requirement) -> bool:
    expected = requirement.expected
    if requirement.predicate == 'equals':
        return type(value) is type(expected) and value == expected
    if requirement.predicate == 'contains':
        return isinstance(value, (str,list)) and expected in value
    if requirement.predicate == 'greater_than':
        return type(value) in (int,float) and type(expected) in (int,float) and value > expected
    return False


def check(req: Requirement, p: Problem, observations: list[Observation], readable: set[str]) -> Requirement:
    rows = [o for o in observations if o.signal == req.signal and o.service == p.service]
    results, ids, reasons = [], [], []
    for o in rows:
        if o.failed:
            results.append('query_failed'); reasons.append(o.reason or '取证失败'); continue
        scope = (o.scope_verified and o.env == p.env and o.cluster == p.cluster
                 and (not p.object_name or o.object_name == p.object_name)
                 and (not o.endpoint or o.endpoint == p.endpoint))
        scope=scope and (not p.instance or (o.instance==p.instance and o.boot_id==p.boot_id))
        scope=scope and (not p.query_fingerprint or o.query_fingerprint==p.query_fingerprint)
        scope=scope and (not p.request_id or o.request_id==p.request_id)
        temporal = o.time_range[0] <= p.first_seen <= o.time_range[1]
        if not scope or not temporal or not o.complete or o.value is None or o.evidence_id not in readable:
            results.append('insufficient_coverage'); reasons.append('作用域/历史覆盖/引用未能验证'); continue
        valid = compare(o.value,req)
        if req.signal == 'ddl_removed_before_fault' and (o.event_time is None or o.event_time > p.first_seen):
            valid = False
        results.append('satisfied' if valid else 'refuted'); ids.append(o.evidence_id)
    if 'satisfied' in results and 'refuted' in results:
        state, reason = 'insufficient_coverage', '支持与反证冲突，不能确认'
    elif 'refuted' in results:
        state, reason = 'refuted', '可靠覆盖范围内条件不成立'
    elif 'satisfied' in results:
        state, reason = 'satisfied', '必要条件已由可回查事实满足'
    else:
        state = 'query_failed' if 'query_failed' in results else ('insufficient_coverage' if rows else 'not_queried')
        reason = '；'.join(dict.fromkeys(reasons)) or req.reason or '尚未取得该条件证据'
    return Requirement.model_validate(req.model_dump() | {'state':state,
        'evidence_ids':sorted(set(ids)), 'reason':reason})


def verify_candidate(c: Candidate, p: Problem, observations: list[Observation], readable: set[str]) -> Candidate:
    requirements = [check(r,p,observations,readable) for r in c.requirements]
    required = [r for r in requirements if r.required]
    supporting = sorted({i for r in requirements if r.state=='satisfied' for i in r.evidence_ids})
    contradicting = sorted({i for r in requirements if r.state=='refuted' for i in r.evidence_ids})
    satisfied = sum(r.state == 'satisfied' for r in required)
    value = satisfied / max(1,len(required))
    status, reason = 'candidate', '必要条件尚未充分验证'
    if contradicting:
        status, reason = 'excluded', '必要条件在可靠覆盖范围内被反证'
    elif required and satisfied == len(required) and set(supporting) - set(c.basis_ids):
        known = (c.origin=='rule' and c.mechanism_key in SPECS and {r.signal for r in required} ==
            set(SPECS[c.mechanism_key][2]) and all(r.predicate=='equals' and r.expected is True
                                                  for r in required))
        attested = any(c.mechanism_key in o.mechanism_keys and o.evidence_id in supporting for o in observations)
        if known or attested:
            status, reason = 'confirmed', '具体验证条件满足，支持引用可回查，无未处理反证'
        else:
            reason = '新机制缺少独立的语义验证记录，仅条件匹配不足以确认因果'
    return Candidate.model_validate(c.model_dump() | {'requirements':requirements,
        'status':status, 'rank':round(value,4), 'sufficiency':round(value,4),
        'supporting':supporting,'contradicting':contradicting,'reason':reason})


def evaluate(snapshot: Snapshot, observations: list[Observation], readable: set[str]) -> Snapshot:
    for p in snapshot.problems:
        p.gaps = [g for g in p.gaps if '：尚未取得' not in g and '：作用域/' not in g
                  and '：必要条件' not in g and '：取证失败' not in g]
        p.candidates = [verify_candidate(c,p,observations,readable) for c in p.candidates]
        deferred=[verify_candidate(c,p,observations,readable) for c in snapshot.candidate_backlog.get(p.problem_id,[])]
        if deferred:snapshot.candidate_backlog[p.problem_id]=deferred
        confirmed = [c for c in p.candidates+deferred if c.status == 'confirmed']
        p.cause = Conclusion()
        if len(confirmed) == 1:
            c = confirmed[0]
            if p.mechanism.status != 'confirmed':
                p.mechanism = Conclusion(status='confirmed',statement=c.statement,evidence_ids=c.supporting)
            p.cause = Conclusion(status='confirmed',statement=c.statement,evidence_ids=c.supporting)
        elif len(confirmed) > 1:
            p.gaps.append('多个机制条件同时成立，尚未区分形成原因')
        p.gaps = list(dict.fromkeys(p.gaps + [r.question + '：' + r.reason for c in p.candidates
            if c.status=='candidate' for r in c.requirements if r.state!='satisfied']))
    from .confidence import refresh
    return refresh(Snapshot.model_validate(snapshot.model_dump()))
