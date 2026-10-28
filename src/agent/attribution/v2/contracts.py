"""归因分层契约。提出原因与验证原因是两种操作，模型不能直接确认。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CheckState = Literal['not_queried', 'satisfied', 'refuted', 'query_failed',
                     'insufficient_coverage']
Level = Literal['fact', 'mechanism', 'cause']


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', revalidate_instances='always')


class Confidence(Record):
    level: Literal['high','medium','low','insufficient'] = 'insufficient'
    method: Literal['evidence-confidence-v1'] = 'evidence-confidence-v1'
    calibrated: Literal[False] = False
    probability: None = None
    rationale: str = Field(default='尚无足够独立证据评估；不是原因正确概率',min_length=1)
    supporting: list[str] = Field(default_factory=list)
    contradicting: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class CauseAssessment(Record):
    candidate_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    status: Literal['provisional','confirmed']
    confidence: Confidence


class Conclusion(Record):
    status: Literal['unconfirmed', 'confirmed'] = 'unconfirmed'
    statement: str = ''
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def traceable(self):
        if self.status == 'confirmed' and (not self.statement or not self.evidence_ids):
            raise ValueError('确认必须有陈述和证据引用')
        return self


class Requirement(Record):
    key: str = Field(min_length=1)
    question: str = Field(min_length=1)
    signal: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    expected: Any = True
    required: bool = True
    tool: str = ''
    params: dict[str, Any] = Field(default_factory=dict)
    state: CheckState = 'not_queried'
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str = ''

    @model_validator(mode='after')
    def traceable(self):
        if self.state in ('satisfied', 'refuted') and not self.evidence_ids:
            raise ValueError('条件已满足/已反证必须可回查')
        return self


class Candidate(Record):
    candidate_id: str = Field(min_length=1)
    problem_id: str = Field(min_length=1)
    mechanism_key: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    category: str = 'other'
    origin: Literal['rule', 'model'] = 'rule'
    basis_ids: list[str] = Field(min_length=1)
    requirements: list[Requirement] = Field(min_length=1)
    status: Literal['candidate', 'confirmed', 'excluded'] = 'candidate'
    rank: float = Field(default=0, ge=0, le=1)
    sufficiency: float = Field(default=0, ge=0, le=1)
    supporting: list[str] = Field(default_factory=list)
    contradicting: list[str] = Field(default_factory=list)
    reason: str = ''
    supersedes: list[str] = Field(default_factory=list)
    confidence: Confidence = Field(default_factory=Confidence)

    @model_validator(mode='after')
    def decision(self):
        required = [r for r in self.requirements if r.required]
        if self.status == 'confirmed' and (not required or not self.supporting or
                any(r.state != 'satisfied' for r in required) or self.contradicting):
            raise ValueError('原因确认需要必要条件、支持和无未处理反证')
        if self.status == 'excluded' and (not self.contradicting or not self.reason):
            raise ValueError('排除需要实际反证与原因')
        return self


class Problem(Record):
    observed_id: str = ''
    problem_id: str = Field(min_length=1)
    service: str = Field(min_length=1)
    env: str = ''
    cluster: str = ''
    error_type: str = Field(min_length=1)
    metric_name: str = ''
    metric_family: str = ''
    metric_type: str = ''
    metric_unit: str | None = None
    metric_statistic: str = ''
    request_id: str = ''
    exception_type: str = ''
    object_name: str = ''
    endpoint: str = ''
    instance: str = ''
    boot_id: str = ''
    query_fingerprint: str = ''
    time_range: tuple[datetime, datetime]
    first_seen: datetime
    samples: list[str] = Field(default_factory=list)
    sample_count: int = Field(default=0, ge=0)
    evidence_ids: list[str] = Field(default_factory=list)
    raw_refs: list[str] = Field(default_factory=list)
    observation_groups: dict[str, str] = Field(default_factory=dict)
    coverage: str = 'bounded_sample'
    current_state: str = 'unknown'
    current_state_note: str = '历史样本不能证明此刻仍发生'
    fact: Conclusion = Field(default_factory=Conclusion)
    mechanism: Conclusion = Field(default_factory=Conclusion)
    cause: Conclusion = Field(default_factory=Conclusion)
    candidates: list[Candidate] = Field(default_factory=list)
    cause_assessments: list[CauseAssessment] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def coherent(self):
        if self.time_range[1] < self.time_range[0]:
            raise ValueError('查询窗口倒置')
        if self.cause.status == 'confirmed' and (self.fact.status != 'confirmed'
                or self.mechanism.status != 'confirmed'):
            raise ValueError('不能越过事实/机制直接确认形成原因')
        if any(c.problem_id != self.problem_id for c in self.candidates):
            raise ValueError('候选跨问题引用')
        return self


class SDKAccessCheck(Record):
    """A real on-demand interface response, separate from historical cause evidence."""
    identity: dict[str, Any]
    incident_id: str
    observer_id: str
    tool: str
    interface_id: str
    checked_at: datetime
    attempt_id: str
    call_id: str
    attempt: int = Field(ge=1)
    ok: bool
    error: str
    http_status: int | None = None
    elapsed_ms: float = Field(ge=0)
    raw_ref: str
    persist_state: str
    query: dict[str,Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def actual_response(self):
        if self.checked_at.tzinfo is None or self.ok != (self.error == 'none'):
            raise ValueError('SDK访问现状需要有时区的实际结果')
        if not self.attempt_id or not self.raw_ref:
            raise ValueError('SDK访问现状必须关联实际尝试记录')
        return self


class Snapshot(Record):
    version: Literal[2] = 2
    score_version: str = 'obligations-v1'
    problems: list[Problem] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    pending_problems: list[str] = Field(default_factory=list)
    stop_reason: str = ''
    notes: list[str] = Field(default_factory=list)
    source: dict[str, Any] = Field(default_factory=dict)
    capability_gaps: list[dict[str, str]] = Field(default_factory=list)
    candidate_backlog: dict[str,list[Candidate]] = Field(default_factory=dict)
    sdk_access_checks: list[SDKAccessCheck] = Field(default_factory=list, max_length=64)
    observability_problems: list[Problem] = Field(default_factory=list)
    sdk_verification: dict[str,Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def backlog_scope(self):
        problems={p.problem_id:p for p in self.problems}
        if sum(map(len,self.candidate_backlog.values()))>64:raise ValueError('待查目录超过上限')
        for pid,items in self.candidate_backlog.items():
            if len(items)>8:raise ValueError('单问题待查目录超过上限')
            if pid not in problems or any(c.problem_id!=pid or
                    not set(c.basis_ids)<=set(problems[pid].evidence_ids) for c in items):
                raise ValueError('待查候选跨问题或引用不存在')
            if any(c.candidate_id in {x.candidate_id for x in problems[pid].candidates} for c in items):
                raise ValueError('候选不能同时处于活跃与待查队列')
        return self


def project_root(snapshot: Snapshot) -> dict[str, Any] | None:
    """只投影单问题已确认的形成原因，不以候选排名冒充确认。"""
    if len(snapshot.problems) != 1 or snapshot.pending_problems:
        return None
    problem = snapshot.problems[0]
    return (problem.cause.model_dump(mode='json') | {'service': problem.service,
            'problem_id': problem.problem_id}) if problem.cause.status == 'confirmed' else None
