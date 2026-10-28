"""One bounded plan/fetch/verify lifecycle for initial and directed diagnosis.

Source access, retry policy and audit remain in Broker/Executor. This application
workflow owns execution order and receipts, never evidence semantics or wording.
"""
import asyncio
import time
from copy import deepcopy
from dataclasses import dataclass
from ..domain.diagnosis import DiagnosisCommand
from ..attribution.v2.planning import plan
from ..attribution.v2.verification import Observation, evaluate
from ..attribution.v2.selection import problem_snapshot
from .diagnosis_budget import budget_reason


@dataclass
class Fetched:
    observations: list
    readable_ids: set[str]
    error_code: str = ''
    raw_ref: str = ''


@dataclass
class DiagnosisOutcome:
    snapshot: object
    query_ids: list[str]
    receipts: list[dict]


def planning_snapshot(snapshot, command):
    if command.mode == 'initial': return snapshot
    selected = problem_snapshot(snapshot.model_dump(mode='json'), command.problem_id)
    p = selected.problems[0]
    if not set(command.candidate_ids) <= {c.candidate_id for c in p.candidates}:
        raise ValueError('定向候选不属于已激活的问题范围')
    p.candidates = [c for c in p.candidates if c.candidate_id in command.candidate_ids]
    if command.requirement_key and not any(r.key == command.requirement_key for c in p.candidates for r in c.requirements):
        raise ValueError('定向条件不属于所选候选')
    for c in p.candidates:
        c.status = 'candidate'  # Plan input only; never mutate the saved verdict.
        for r in c.requirements:
            r.state = 'not_queried' if not command.requirement_key or r.key == command.requirement_key else 'insufficient_coverage'
    return selected


class Execution:
    def __init__(self, snapshot, command, observations, readable, visited, budget):
        self.snapshot, self.command = snapshot.model_copy(deep=True), command
        self.rows, self.readable = deepcopy(list(observations)), set(readable)
        self.visited = set() if command.recheck else set(visited)
        self.receipts, self.actual = [], []
        self.budget = budget
        self.started, self.base = time.monotonic(), budget.elapsed_sec
        planning_snapshot(self.snapshot, command)

    def observe(self):
        self.budget.observe_elapsed(self.base + time.monotonic() - self.started)

    async def batch(self, queries, planning, fetch):
        before = len(self.rows)
        for query in queries:
            self.observe()
            if budget_reason(self.budget): break
            if not self.budget.rounds: self.budget.spend_round()
            self.visited.add(query.query_id); self.actual.append(query.query_id)
            got, receipt = await execute(query, planning, fetch, self.budget)
            self.receipts.append(receipt)
            self.rows.extend(got.observations); self.readable.update(got.readable_ids)
            targets = {pid for pid, _, _ in query.requirements}
            for p in self.snapshot.problems:
                if p.problem_id not in targets: continue
                p.evidence_ids = list(dict.fromkeys(p.evidence_ids + [o.evidence_id for o in got.observations
                    if o.service == p.service and o.evidence_id]))
        return len(self.rows) > before and not all(o.failed for o in self.rows[before:])

    def finish(self, stop):
        self.snapshot = assess(self.snapshot, self.rows, self.readable, self.command)
        self.snapshot.observations = [o.model_dump(mode='json') for o in self.rows]
        self.observe()
        self.snapshot.stop_reason = budget_reason(self.budget) or stop
        workflow = dict(version=1, command=self.command.data(), queries=self.actual, receipts=self.receipts,
                        stop_reason=self.snapshot.stop_reason, logical_queries=len(self.actual),
                        tool_attempts=sum(r['tool_attempts'] for r in self.receipts))
        self.snapshot.source['diagnosis_workflow'] = workflow
        self.snapshot.history.append({'round': self.budget.rounds, 'final': True,
            'stop_reason': self.snapshot.stop_reason, 'workflow': workflow, 'problems': verdicts(self.snapshot)})
        return DiagnosisOutcome(self.snapshot, self.actual, self.receipts)


async def run(snapshot, *, command=DiagnosisCommand(), tools, fetch, readable, budget,
              observations=(), generate=None, on_update=None, on_plan=None, availability=None, visited=()):
    execution = Execution(snapshot, command, observations, readable, visited, budget)
    stop = '诊断轮次上限'
    for iteration in range(command.max_iterations):
        execution.observe()
        if (iteration or not budget.rounds) and not budget.has_round_left():
            stop = '轮次预算耗尽'; break
        if iteration: budget.spend_round()
        if not execution.snapshot.problems:
            stop = '请求窗口内没有可定位的自身故障样本'; break
        if generate and command.mode == 'initial' and not budget_reason(budget):
            await expand(execution, generate)
        execution.snapshot = assess(execution.snapshot, execution.rows, execution.readable, command)
        snapshot = execution.snapshot
        if command.mode == 'initial':
            snapshot.history.append({'round': budget.rounds, 'problems': verdicts(snapshot)})
        if on_update: await on_update(snapshot)
        if command.mode == 'initial' and snapshot.problems and all(p.cause.status == 'confirmed' for p in snapshot.problems):
            stop = '验证条件已收敛'; break
        if not snapshot.problems:
            stop = '请求窗口内没有可定位的自身故障样本'; break
        reason = budget_reason(budget)
        if reason: stop = reason; break
        planning = planning_snapshot(snapshot, command)
        if on_plan: on_plan(planning)
        limit = min(command.batch_size, command.max_queries - len(execution.actual), budget.max_tool_calls - budget.tool_calls)
        queries = plan(planning, tools, execution.visited, limit=limit, availability=availability)
        copy_gaps(snapshot, planning)
        if not queries:
            stop = '无新可执行查询；保留能力缺口/覆盖不足'; break
        if not await execution.batch(queries, planning, fetch):
            stop = '无新增可验证事实，停止重复查询'; break
        if command.mode == 'directed':
            pending=plan(planning,tools,execution.visited,limit=1,availability=availability)
            stop=('本轮定向查询达到额度，仍有条件待核查；按实际见证更新。' if pending else
                  '定向核查已完成，按实际见证更新条件。')
    result = execution.finish(stop)
    if on_update: await on_update(result.snapshot)
    return result


async def expand(execution, generate):
    execution.observe()
    remaining = execution.budget.max_wall_time_sec - execution.budget.elapsed_sec
    try:
        await asyncio.wait_for(generate(execution.snapshot), timeout=max(.001, remaining))
    except TimeoutError:
        execution.snapshot.notes.append('候选生成超过剩余耗时预算；保留已有事实与候选')
    finally: execution.observe()


def assess(snapshot, rows, readable, command):
    if command.mode == 'initial': return evaluate(snapshot, rows, readable)
    # A directed query can revise its problem, never another problem's verdict.
    focused = problem_snapshot(snapshot.model_dump(mode='json'), command.problem_id)
    checked = evaluate(focused, rows, readable)
    snapshot.problems = [checked.problems[0] if p.problem_id == command.problem_id else p for p in snapshot.problems]
    if command.problem_id in checked.candidate_backlog:
        snapshot.candidate_backlog[command.problem_id] = checked.candidate_backlog[command.problem_id]
    return snapshot


def verdicts(snapshot):
    return [{'problem_id': p.problem_id, 'cause': p.cause.model_dump(mode='json'),
             'candidates': [c.model_dump(mode='json') for c in p.candidates]} for p in snapshot.problems]


def copy_gaps(snapshot, planning):
    if snapshot is planning: return
    for p in planning.problems:
        original = next(x for x in snapshot.problems if x.problem_id == p.problem_id)
        original.gaps = list(dict.fromkeys(original.gaps + p.gaps))
        for c in p.candidates:
            saved = next(x for x in original.candidates if x.candidate_id == c.candidate_id)
            for r in c.requirements:
                next(x for x in saved.requirements if x.key == r.key).reason = r.reason
    for gap in planning.capability_gaps:
        if gap not in snapshot.capability_gaps: snapshot.capability_gaps.append(deepcopy(gap))


async def execute(query, planning, fetch, budget):
    from ..observed.attempts import safe
    started, base, before = time.monotonic(), budget.elapsed_sec, budget.tool_calls
    budget.spend_tool()  # Broker passes pre_reserved=True; retries reserve themselves.
    error = ''
    try:
        got = await asyncio.wait_for(fetch(query), timeout=max(.001, budget.max_wall_time_sec - base))
        got = Fetched([Observation.model_validate(o) for o in got.observations], set(got.readable_ids),
                      getattr(got, 'error_code', ''), getattr(got, 'raw_ref', ''))
    except Exception as exc:
        error = 'TIMEOUT' if isinstance(exc, TimeoutError) else type(exc).__name__
        got = Fetched(failed_rows(query, planning, error), set())
    finally: budget.observe_elapsed(base + time.monotonic() - started)
    state = 'failed' if error or got.observations and all(o.failed for o in got.observations) else (
        'empty' if not got.observations else 'observed')
    receipt = dict(query_id=query.query_id, tool=query.tool, parameters=safe(query.params), requirements=[list(r) for r in query.requirements],
        query_spec=query.query_spec.summary() if query.query_spec else None, outcome=state,
        error=error or got.error_code, raw_ref=safe(got.raw_ref), tool_attempts=budget.tool_calls - before,
        elapsed_ms=round((time.monotonic()-started)*1000, 1),
        observations=[dict(evidence_id=o.evidence_id, signal=o.signal, failed=o.failed,
                           complete=o.complete, scope_verified=o.scope_verified, reason=o.reason) for o in got.observations])
    return got, receipt


def failed_rows(query, snapshot, reason):
    rows = []
    for pid, cid, key in query.requirements:
        p = next(p for p in snapshot.problems if p.problem_id == pid)
        c = next(c for c in p.candidates if c.candidate_id == cid)
        r = next(r for r in c.requirements if r.key == key)
        rows.append(Observation(signal=r.signal, service=p.service, object_name=p.object_name,
            env=p.env, cluster=p.cluster, time_range=p.time_range, failed=True, reason='取证失败：'+reason))
    return rows
