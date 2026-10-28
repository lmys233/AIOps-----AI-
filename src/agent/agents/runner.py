"""取证运行器 —— 5 个 Agent **并行**取证，产物全部汇入黑板。

--------------------------------------------------------------------------------
★ 一次取证运行的五步（顺序即不变量）
--------------------------------------------------------------------------------

| 步 | 做什么 | 为什么必须在这一步 |
|---|---|---|
| 1 | `board.create`（幂等） | 后面所有留痕都要有黑板可写；重放不能产生第二块黑板 |
| 2 | 定观测窗口（`slots.time_window` → `[start, end]`） | 窗口是**所有**取证 Agent 共用的同一个事实（各自算会算出 5 个窗口） |
| 3 | `EvidencePlanner.plan(...)` → 可填充性检查 | ★ 发调用**之前**就知道"哪些路发得出去、缺什么" |
| 4 | 并行执行计划里的调用 | 5 路**互不等待**；一路挂了不影响其余（降级优于失败） |
| 5 | 证据/空结果/降级**分类落黑板** | "查了没异常"与"没查成"必须在黑板上留下不同的痕迹 |

--------------------------------------------------------------------------------
★ 为什么发布在黑板上由**运行器**做，而不是每个 Agent 自己做
--------------------------------------------------------------------------------

· Agent 手里没有黑板句柄 → 它**想**绕过流程写别人的证据也无从下手（
  "隔离必须是接口层面的事实"，不是"大家自觉"）；
· `publish` 要按角色署名，运行器是唯一知道"这次是谁的产物"的地方；
· 一个 Agent 的多次调用要**先合并**再发布，否则同一角色会散成好几条时间线记录。

--------------------------------------------------------------------------------
★ 降级要"响"（不是静默）
--------------------------------------------------------------------------------

本运行器**从不**因为一路失败而抛异常结束：
  · 计划期判定的缺失 → `mark_degraded`；
  · 执行期工具失败 → `mark_degraded`；
  · 连 `build_evidence` 自己炸了（实现 bug）→ 也 `mark_degraded` 并 `logger.exception`。
每一路缺失都**写在黑板上**（RCA 打分要把它当修正项），同时写日志给人看。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Iterable, Mapping, Optional, Sequence

from ..attribution.errors import ordered_trace_ids
from ..board.store import BoardStore
from ..common.contracts import Evidence, Slots
from ..tools.executor import ToolExecutor
from ..tools.semantics import parse_duration
from ..common.steps import emit_step
from .base import AgentContext, AgentOutcome, ForensicsAgent, ToolRequest
from .change_agent import ChangeAgent
from .log_agent import LogAgent
from .metrics_agent import MetricsAgent
from .planner import EvidencePlanner, PendingGap, PlanOutcome
from .topology_agent import TopologyAgent
from .trace_agent import TraceAgent

__all__ = [
    "ForensicsRun",
    "ForensicsRunner",
    "default_agents",
    "resolve_window",
    "DEFAULT_WINDOW_SEC",
    "MAX_PARALLEL_CALLS",
]

logger = logging.getLogger("agent.agents.runner")

#: 槽位没给时间窗时的默认观测窗口（与 `promql_read` schema 的默认值保持一致）
DEFAULT_WINDOW_SEC: Final[int] = 1800

#: 同时在飞的工具调用上限（5 路 × 每路若干次调用，别一次全打出去）
MAX_PARALLEL_CALLS: Final[int] = 8


def default_agents() -> list[ForensicsAgent]:
    """5 个取证 Agent（顺序固定，便于日志与断言可复现）。"""
    agents=[MetricsAgent(), LogAgent(), TraceAgent(), ChangeAgent(), TopologyAgent()]
    from ..diagnostics.wiring import enabled
    if enabled():
        from .diagnostic_agents import diagnostic_agents
        agents.extend(diagnostic_agents())
    from ..observed.access_agent import SDKAccessAgent
    from ..observed.context import enabled as multi_system_enabled
    if multi_system_enabled():agents.append(SDKAccessAgent())
    return agents


def resolve_window(slots: Slots, *, now: Optional[datetime] = None
                   ) -> tuple[datetime, datetime]:
    """`slots.time_window`（如 `30m`）→ `[start, end]`。

    ★ 时间窗的解析**只有一处**（`semantics.parse_duration`）：
      调度层自己再写一套 `30m` 解析，迟早会出现"工具查 30 分钟、黑板上写 45 分钟"。
    """
    seconds = parse_duration(slots.time_window) or DEFAULT_WINDOW_SEC
    from ..execution.runtime import CURRENT
    execution=CURRENT.get()
    end = now or (datetime.fromisoformat(execution.body['started_at']) if execution else datetime.now(timezone.utc))
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return end - timedelta(seconds=seconds), end


def query_context(incident_id,slots,start,end,tenant_id):
    from ..observed.context import current
    from ..queries.binding import scope_of
    ctx=AgentContext(incident_id=incident_id,slots=slots,window_start=start,window_end=end,tenant_id=tenant_id)
    return replace(ctx,query_scope=scope_of(current(),ctx.services))


# ======================================================================
@dataclass
class ForensicsRun:
    """一次取证运行的**全部产物**（黑板之外的进程内视图，便于单测与复盘）。"""

    incident_id: str
    plan: PlanOutcome
    outcomes: list[AgentOutcome] = field(default_factory=list)
    published: dict[str, int] = field(default_factory=dict)      # 角色 → 新增证据条数
    duplicates: list[str] = field(default_factory=list)
    wall_ms: float = 0.0
    coverage: dict = field(default_factory=dict)
    metric_localization: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    def outcome_of(self, role: str) -> Optional[AgentOutcome]:
        return next((item for item in self.outcomes if item.role == role), None)

    @property
    def evidence(self) -> list[Evidence]:
        """全部证据（**顺序稳定**：按角色、再按产出顺序）。"""
        out: list[Evidence] = []
        for outcome in self.outcomes:
            out.extend(outcome.evidence)
        return out

    @property
    def degraded_signals(self) -> list[str]:
        """这一轮"没查成"的**数据源级**信号（闭集 = `EvidenceSource`）。

        ★★ 为什么必须是源级而不是细信号名：
          `AgentState.degraded_signals` 用 `{m.value for m in EvidenceSource}` 校验，
          写进 `pool_waiting` / `combine` 这类名字会**直接抛错** ——
          于是"缺失信号上报"这条链路恰好会在它该生效的时候崩掉。
          细信号与人话原因留在 `degraded_detail` 里（给人看，不进契约字段）。
        """
        names: list[str] = list(self.plan.degraded_sources)
        for outcome in self.outcomes:
            names.extend(outcome.degraded_sources)
        seen: list[str] = []
        for name in names:
            if name and name not in seen:
                seen.append(name)
        return seen

    @property
    def degraded_detail(self) -> list[str]:
        """细信号 + 原因（**诊断用**，不进契约字段）。"""
        lines: list[str] = []
        for signal, reason in self.plan.degraded:
            lines.append(f"{signal}：{reason}")
        for outcome in self.outcomes:
            lines.extend(outcome.degraded)
        return lines

    @property
    def empty_signals(self) -> list[str]:
        """这一轮"查了没异常"的信号（**不是**缺失）。"""
        names: list[str] = []
        for outcome in self.outcomes:
            for name in outcome.empty:
                if name not in names:
                    names.append(name)
        return names

    @property
    def gaps(self) -> list[PendingGap]:
        return list(self.plan.gaps)

    @property
    def qps_estimates(self) -> dict[str, float]:
        merged: dict[str, float] = {}
        for outcome in self.outcomes:
            merged.update(outcome.qps_estimates)
        return merged

    def to_dict(self) -> dict[str, Any]:
        return {"incident_id": self.incident_id,
                "plan": self.plan.plan.model_dump(mode="json"),
                "calls": len(self.plan.calls),
                "outcomes": [item.to_dict() for item in self.outcomes],
                "published": dict(self.published),
                "duplicates": list(self.duplicates),
                "degraded_signals": self.degraded_signals,
                "degraded_detail": self.degraded_detail,
                "empty_signals": self.empty_signals,
                "unresolved": self.plan.unresolved,
                "qps": self.qps_estimates,
                "query_coverage": self.coverage,
                "metric_localization": self.metric_localization,
                "wall_ms": round(self.wall_ms, 1)}


# ======================================================================
# ======================================================================
def _merge_results(jobs: Sequence[Any],
                   results: Sequence[Any]) -> dict[str, AgentOutcome]:
    """`asyncio.gather(..., return_exceptions=True)` 的产物 → 按角色合并的结局。

    ★ 异常在这里翻译成**降级**（"这一路没查成"），而不是让整轮排查一起死；
      `CancelledError` 是唯一的例外：它是**调用方要求停止**，必须继续往上抛，
      否则"超时/预算终止"会被降级路径吞掉（那正是编排的终止条件之一）。
    """
    merged: dict[str, AgentOutcome] = {}
    for (agent, call, _call_id), item in zip(jobs, results):
        if isinstance(item, BaseException):
            if isinstance(item, asyncio.CancelledError):
                raise item
            logger.error("取证调用以异常结束（%s/%s）：%s", agent.role, call.tool, item)
            outcome = AgentOutcome(role=agent.role)
            outcome.degraded.append(
                f"{call.signal or call.tool}：没查成（{type(item).__name__}）："
                f"{str(item)[:120]}")
            outcome.degraded_sources.append(agent.source.value)
        else:
            outcome = item
        merged.setdefault(outcome.role, AgentOutcome(role=outcome.role)).merge(outcome)
    return merged


def _merge_followup(outcome: AgentOutcome, request: ToolRequest,
                    extra: AgentOutcome) -> None:
    """把一次补充调用的产物按字段并回本轮（**哪些能并、哪些不能**见下）。

    · `calls` / `aux`：一定并（取数留痕与说明必须看得见）；
    · `empty` / `degraded`：**不并** —— 补充手段的结局不许混进"这一路没异常"，
      那会让 RCA 以为少了一路遥测；
    · ★★ `evidence`：**只并"声明为证据"的那些**（`trace_read` 下钻就是这一类：
      它的产物本身就是证据）。修前这里一律不并 —— 真机实测的后果是三条
      `trace_read` 全部 HTTP 200、断点也判出来了，报告里却 **0 条**
      `trace_breakpoint`："取到了"被静默丢掉，比"没查到"更坏。
    """
    if extra.evidence:
        if request.is_evidence:
            outcome.evidence.extend(extra.evidence)
        else:
            outcome.aux.append(
                f"{request.signal or request.tool}：辅助调用产出了 {len(extra.evidence)}"
                f" 条证据，按 `purpose=aux` **不计入**（辅助数据的结局不许混进"
                f"「这一路没异常」）")
    outcome.calls.extend(extra.calls)
    outcome.aux.extend(extra.aux)


def _evidence_of(outcomes: Iterable[AgentOutcome]) -> list[Evidence]:
    """若干角色的证据拉平成一串（**顺序 = 角色顺序**，便于断言与复现）。"""
    return [item for outcome in outcomes for item in outcome.evidence]


class ForensicsRunner:
    """按计划并行取证，并把结局分类落黑板。"""

    def __init__(self, executor: ToolExecutor, board: BoardStore, *,
                 agents: Optional[Sequence[ForensicsAgent]] = None,
                 planner: Optional[EvidencePlanner] = None,
                 max_parallel: int = MAX_PARALLEL_CALLS) -> None:
        self.executor = executor
        self.board = board
        self.agents: list[ForensicsAgent] = list(agents) if agents else default_agents()
        self.planner = planner or EvidencePlanner(executor.spec_of,registry=getattr(executor,'registry',None))
        self._semaphore = asyncio.Semaphore(max(1, max_parallel))

    @property
    def roles(self) -> list[str]:
        return [agent.role for agent in self.agents]

    # ------------------------------------------------------------------
    async def run(self, incident_id: str, slots: Slots, *,
                  window: Optional[tuple[datetime, datetime]] = None,
                  tenant_id: str = "default",
                  asked_gaps: Iterable[str] = (),
                  on_event: Optional[Any] = None) -> ForensicsRun:
        started = time.monotonic()
        await self.board.create(incident_id, slots)

        start, end = window or resolve_window(slots)
        # ★★ `on_event` 挂到 **ctx**（每次 run 一个新对象）而不是 runner 实例上：
        #   runner 会被并发事件复用，实例状态会让两次排查的事件互相串味。
        ctx=query_context(incident_id,slots,start,end,tenant_id)
        from .diagnostic_agents import selected
        planned_agents=[a for a in self.agents if a.role not in ('database_agent','runtime_agent','lifecycle_agent') or selected(a.role,slots.symptom or '')]
        from .metric_discovery import prepare
        ctx,discovery=await prepare(self,ctx,planned_agents,on_event)

        if asked_gaps:
            # ★ 已经问过的缺口不再重复问（编排层的记账传进来；这一层不做预算裁决）。
            #   ★ 作为 `plan()` 的**参数**传下去，不写进规划器实例：
            #     复用一个 runner 处理并发事件时，实例状态会让两个事件的记账互相污染。
            plan = self.planner.plan(ctx, planned_agents, asked=asked_gaps)
        else:
            plan = self.planner.plan(ctx, planned_agents)
        from .admission import admit
        admit(plan)
        await self._record_plan(incident_id, plan)
        # ★★ 参数要说清"**几个 Agent、各查什么**"：修前这里传的是 `[call.role for call in
        #   plan.calls]` —— 一个角色查 12 个指标就印出 12 个同一个名字，用户会读成
        #   "为什么要请求 12 次一样的 role"（真机反馈）。所以：
        #     · `agents` 只列**去重后**的角色；`calls` 是调用次数；`signals` 是这次要查的信号。
        agents_used = sorted({call.role for call in plan.calls})
        emit_step(on_event, "plan", "规划取证",
                  {"agents": agents_used, "calls": len(plan.calls),
                   "signals": sorted({call.signal for call in plan.calls if call.signal}),
                   "tools": sorted({call.tool for call in plan.calls})},
                  result=f"{len(agents_used)} 个 Agent（{'、'.join(agents_used) or '无'}）"
                         f"计划 {len(plan.calls)} 次工具调用；要查的信号："
                         f"{'、'.join(sorted({call.signal for call in plan.calls if call.signal})) or '（无）'}")

        outcomes = await self._execute(ctx, plan, on_event)
        if discovery is not None:
            existing=next((o for o in outcomes if o.role==discovery.role),None)
            if existing is None:outcomes.append(discovery)
            else:existing.merge(discovery)
        from .coverage import describe
        coverage=describe(ctx,planned_agents,plan,outcomes)
        emit_step(on_event,'query_coverage','核对取证覆盖',{},result=coverage)
        from .metric_profiles import describe as metric_priorities
        metric_localization=metric_priorities(ctx,outcomes)
        if metric_localization['profiles']:
            emit_step(on_event,'metric_localization','计算服务核查优先级',
                {'profiles':len(metric_localization['profiles'])},
                result={k:v for k,v in metric_localization.items() if k!='profiles'})
        published, duplicates = await self._publish(incident_id, outcomes)
        emit_step(on_event, "board", "证据入黑板",
                   {"published": sum(published.values()), "duplicates": len(duplicates)},
                   result=f"新增 {sum(published.values())} 条证据"
                          f"（重复 {len(duplicates)} 条按 id 去重）")

        return ForensicsRun(
            incident_id=incident_id, plan=plan, outcomes=outcomes,
            published=published, duplicates=duplicates,
            wall_ms=(time.monotonic() - started) * 1000,coverage=coverage,metric_localization=metric_localization)

    # ------------------------------------------------------------------
    async def _record_plan(self, incident_id: str, plan: PlanOutcome) -> None:
        """把计划里**已知的**缺失/缺口落黑板（在执行之前）。"""
        for gap in plan.gaps:
            # ★ 缺口进 open_questions：编排层从这里取，而不是各 Agent 各问一遍
            await self.board.raise_questions(incident_id, [gap.question])
        if plan.unresolved:
            await self.board.record_signal_checked(
                incident_id, "orchestrator", "unresolved_slots",
                "需要用户提供：" + "、".join(plan.unresolved))
        for signal, reason in plan.degraded:
            await self.board.mark_degraded(incident_id, signal, reason)

    async def _execute(self, ctx: AgentContext, plan: PlanOutcome,
                       on_event: Optional[Any] = None) -> list[AgentOutcome]:
        """按计划并行执行；每个角色的多次调用**先合并**再返回。

        ★★ `return_exceptions=True` 是**必须的**：
          没有它时，只要有一个任务抛出（例如构造 `ToolRequest` 时踩到畸形字段），
          `gather` 就会整体抛出 → **整轮排查终止**，而兄弟任务的异常还会变成
          "Task exception was never retrieved"（连日志都不完整）。
          本运行器的契约是"一路失败不带走整轮"，这里必须真的做到。
        """
        by_role = {agent.role: agent for agent in self.agents}
        # ★★ `call_id` 在这里铸（同一次排查内唯一）：SSE 的 `tool` 事件靠它把
        #   `done` 贴回对应的 `start` —— 并发调用**不能**靠"同名 FIFO"配对。
        jobs = [(by_role[call.role], call, f"{call.role}-{index}")
                for index, call in enumerate(plan.calls, start=1)]
        results = await asyncio.gather(
            *(self._one_call(agent, ctx, call, call_id, on_event)
              for agent, call, call_id in jobs),
            return_exceptions=True) if jobs else []

        merged = _merge_results(jobs, results)

        # ★★ 同一轮全部证据里按"错误条数降序 / 首现"排好的 trace_id ——
        #   下钻的入参只能从这里来（它长在**日志**那一路的证据里）。运行器是唯一
        #   看得见全部角色的地方，所以由它转交，而不是让 Trace Agent 去翻别人的证据。
        drilled = ordered_trace_ids(_evidence_of(merged.values()))
        for role, outcome in merged.items():
            agent = by_role.get(role)
            if agent is not None and outcome.results:
                self._combine(ctx, agent, outcome)
            # ★★ 补充手段（入参要等本轮结果才知道的那类：`raw_read` 的 `raw_ref`、
            #   `trace_read` 的 `trace_id`）—— **本轮内**就跑，不留给下一轮
            await self._run_followups(ctx, agent, outcome, on_event, hints=drilled)

        from .diagnostic_agents import after_discovery
        await after_discovery(self,ctx,merged,on_event)
        return [merged[agent.role] for agent in self.agents if agent.role in merged]

    @staticmethod
    def _combine(ctx: AgentContext, agent: ForensicsAgent, outcome: AgentOutcome) -> None:
        """把多次结果汇总成证据（**失败不能带走整轮**）。"""
        try:
            groups={}
            for key,value in outcome.results.items():
                signal,_,batch=key.partition('@batch:')
                groups.setdefault(batch,{})[signal]=value
            for values in groups.values():outcome.evidence.extend(agent.combine(ctx,values))
        except Exception as exc:  # noqa: BLE001
            logger.exception("角色 %s 的汇总（combine）失败", agent.role)
            # ★ 信号名用**数据源级**：`combine` 这种内部步骤名不是一路信号源，
            #   混进 `degraded_signals` 会让上报链路抛错。
            outcome.degraded.append(
                f"{agent.source.value}：汇总（combine）失败"
                f"（{type(exc).__name__}）：{str(exc)[:120]}")
            outcome.degraded_sources.append(agent.source.value)

    async def _run_followups(self, ctx: AgentContext, agent: Optional[ForensicsAgent],
                             outcome: AgentOutcome, on_event: Optional[Any] = None,
                             hints: Sequence[str] = ()) -> None:
        """跑该角色声明的补充调用（**失败只记 aux，不影响这一路的结论**）。

        ★ 为什么入参要等本轮结果：`raw_read` 的 `raw_ref`、`trace_read` 的 `trace_id`
          都是**查询结果带出来的**，规划阶段（`plan_calls`）根本还没有，写在那里只能是编的。
        ★★ `hints` = 本轮全部证据里排好序的 trace_id。**它必须在同一轮内转交** ——
          等下一轮时，真机第 1 轮就已经定案收口，下钻**永远不会触发**（实测如此）。
        ★ 它**默认不产生证据**（`purpose=aux`）：补充手段的结局不许混进
          "这一路没异常"，也不许变成 `degraded`（那会让 RCA 以为少了一路遥测）。
        ★★ 例外是 `trace_read` 这类**声明为 `purpose="evidence"`** 的下钻：
          它的产物就是证据本身（断点），必须并进本轮产物 —— 只发不并等于白查。
        """
        if agent is None:
            return
        try:
            requests = agent.followup_requests(ctx, outcome, hints=hints)
        except Exception as exc:  # noqa: BLE001 —— 补充手段坏了不影响结论
            logger.exception("角色 %s 的补充调用规划失败", agent.role)
            outcome.aux.append(f"{agent.role}：补充取数规划失败（{type(exc).__name__}）")
            return
        for index, request in enumerate(requests, start=1):
            # ★ 补充调用也要有唯一编号（前端同样靠它配对 start/done）；
            #   `ToolRequest` 是 frozen 的 → 用 `replace` 造一份带编号的（不改原对象）。
            if not request.call_id:
                request = replace(request, call_id=f"{agent.role}-followup-{index}")
            try:
                from ..queries.binding import for_request
                definition=self.executor.spec_of(request.tool)
                if definition:request=replace(request,query_spec=for_request(ctx,request,definition))
                async with self._semaphore:
                    extra = await agent.run_call(ctx, request, self.executor,
                                                 on_event=on_event)
            except Exception as exc:  # noqa: BLE001 —— 同上：只记 aux
                logger.exception("角色 %s 的补充调用 %s 失败", agent.role, request.tool)
                outcome.aux.append(
                    f"{request.signal or request.tool}：补充取数没成（{type(exc).__name__}）")
                continue
            self._keep_raw(agent, request, extra)
            _merge_followup(outcome, request, extra)

    @staticmethod
    def _keep_raw(agent: ForensicsAgent, request: ToolRequest, extra: AgentOutcome) -> None:
        """把下钻回来的**原始 span 树**留在角色上（下一步"候选由代码枚举"要用它）。

        ★ 为什么不能只靠证据：证据里只有一句断点结论（"断点在 inventory/GET /check"），
          而候选、标记（`intrinsic` / `propagated` / `slow`）与 trace JSON 都需要
          **整棵树**。取到了却丢掉原始返回，等于把已经付过钱的结果扔了。
        ★ 存不下/角色不认这一手（替换实现）→ 不报错、不影响本轮：这只是"知道了更好"，
          少了它结论仍然由地板给出。
        """
        if not request.is_evidence or request.tool != "trace_read":
            return
        keep = getattr(agent, "store_trace", None)
        if not callable(keep):
            return
        for result in extra.results.values():
            keep(result)

    async def _one_call(self, agent: ForensicsAgent, ctx: AgentContext, call: Any,
                        call_id: str = "", on_event: Optional[Any] = None) -> AgentOutcome:
        """执行一次计划内的调用（**异常也必须变成降级**，不能带走整轮）。

        ★ 整个函数体都在 `try` 里 —— 包括 `ToolRequest` 的构造与信号量获取：
          以前它们在外面，一个畸形字段就会穿透出去把整轮带走。
        """
        outcome = AgentOutcome(role=agent.role)
        try:
            request = ToolRequest(tool=call.tool, params=dict(call.params),
                                  signal=call.signal, rationale=call.rationale,
                                  purpose=call.purpose, call_id=call_id,query_spec=getattr(call,'query_spec',None))
            async with self._semaphore:
                if getattr(call,'batch_services',()):
                    from .batches import contexts
                    slots=ctx.slots.model_copy(update={'service_names':list(call.batch_services),
                        'service':ctx.slots.service if ctx.slots.service and ctx.slots.service.name in call.batch_services else None})
                    ctx=replace(ctx,slots=slots)
                outcome=await agent.run_call(ctx, request, self.executor, on_event=on_event)
                for row in outcome.calls:
                    row['batch_services']=list(getattr(call,'batch_services',()) or ctx.services)
                    row['batch_index']=getattr(call,'batch_index',0)
                if getattr(call,'batch_index',0):
                    outcome.results={key+'@batch:'+str(call.batch_index):value for key,value in outcome.results.items()}
                return outcome
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s 执行 %s 时发生未预期异常", agent.role, call.tool)
            outcome.degraded.append(
                f"{call.signal or call.tool}：没查成（{type(exc).__name__}）："
                f"{str(exc)[:120]}")
            outcome.degraded_sources.append(agent.source.value)
            return outcome

    async def _publish(self, incident_id: str,
                       outcomes: Sequence[AgentOutcome]) -> tuple[dict[str, int], list[str]]:
        """按角色发布证据 + 记"查过"与"没查成"（**黑板是唯一共享状态**）。"""
        published: dict[str, int] = {}
        duplicates: list[str] = []
        for outcome in outcomes:
            if outcome.evidence:
                result = await self.board.publish(incident_id, outcome.role, outcome.evidence)
                published[outcome.role] = result.added
                duplicates.extend(result.duplicates)
                await self.board.record_signal_checked(
                    incident_id, outcome.role, "evidence_published",
                    f"{result.added} 条证据")
            for signal in outcome.empty:
                # ★ "查了没异常"：这是**排除假设的依据**，必须与"没查成"分开记
                await self.board.record_signal_checked(
                    incident_id, outcome.role, signal, "empty")
            for line in outcome.degraded:
                signal, _, reason = line.partition("：")
                await self.board.mark_degraded(incident_id, signal or outcome.role,
                                               reason or line)
            if outcome.qps_estimates:
                await self.board.set_qps(incident_id, outcome.qps_estimates)
        return published, duplicates
