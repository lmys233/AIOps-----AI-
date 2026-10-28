"""取证规划 `EvidencePlan` —— **可填充性检查在做工具调用之前**。

--------------------------------------------------------------------------------
★ 为什么这件事必须在这里做，而不是在意图层
--------------------------------------------------------------------------------

意图层回答的是：「用户说的这些，够不够我确定他**想干什么**？」
  → 它判的是**槽位**（`SLOT_REQUIREMENTS`：排查必须知道哪个服务）。
取证规划回答的是：「按这个意图我打算调的**那些工具**，参数**填得出来**吗？」
  → 它判的是**工具入参**（`ToolSpec.input_schema` 的 `required`）。

★ 把这两件事混在一起的两种翻车方式：

| 混法 | 后果 |
|---|---|
| 让意图层判工具入参 | 模型自己生成的 `promql_read.query` 会被当成"要问用户的槽位" —— 用户被问「请提供 PromQL 语句」 |
| 让每个工具各自反问 | 9 个工具 9 种问法；同一个缺口（如 `change_id`）变成 N 条重复的澄清问题 |

所以本模块的产出是**一份计划 + 三种处置**，而且**处置顺序不可变**：

| 顺序 | 情形 | 处置 |
|---|---|---|
| 1 | 能从**槽位 / 上下文 / 配置 / 黑板**填 | 自己填，正常执行 |
| 2 | 只能由**用户**提供（如 `change_id`） | 写成待决项（`PendingGap`），回写 `unresolved`；**不发出工具调用** |
| 3 | 谁都给不了 | 该路标记 `degraded` 继续（缺失信号进打分的修正项） |

★ 两个硬约束：

· **第 2 种情形绝不"先硬调一次再吃 `TOOL_INVALID_PARAMS`"** —— 那是拿用户的时间
  换一句必然失败的报错。
· **同一个缺口只产出一条待决项**：`PendingGap` 按 `field` 去重，
  而不是按"发现它的那个 Agent"各来一条。

★ 本模块**不**决定"要不要问用户、问几次" —— 那是编排层的预算裁决。
  这里只如实报告"我缺这个参数，而且它只能由用户给"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Final, Iterable, Mapping, Optional, Sequence

from ..common.contracts import EvidencePlan
from ..common.enums import EvidenceSource
from ..intent.slots import ACTIVE_SLOTS
from ..tools.schema_check import validate_params
from .base import AgentContext, ForensicsAgent, ToolRequest

__all__ = [
    "FIELD_SOURCES",
    "USER_ONLY_FIELDS",
    "TOOL_SIGNALS",
    "PlannedCall",
    "PendingGap",
    "PlanOutcome",
    "EvidencePlanner",
    "GAP_QUESTIONS",
]

# ======================================================================
#  ① 字段 → 谁能提供（这张表就是"可填充性检查"的依据）
# ======================================================================
#: 取值含义：
#:   `slots`     用户/意图层已经抽好的槽位（`AgentContext.slots`）
#:   `context`   本次排查的上下文（时间窗、目标实体、租户）
#:   `config`    配置里的默认值（集群名、默认时间窗）
#:   `generated` **由取证 Agent 自己生成**（PromQL/LogQL/TraceQL 文本、动作类别）
#:   `board`     黑板上已经存在的东西（如待反查的 `evidence_id`）
#:   `user`      ★ **只能由用户提供** —— 数据源里根本无从猜起
#:
#: ★ 这张表**故意写全**而不是用 `dict.get(field, "unknown")` 兜底：
#:   第 3 种处置（谁都给不了）必须是"表里明确写了没人能给"，
#:   而不是"我忘了登记这个字段" —— 后者是 bug，不该被当成正常降级。
FIELD_SOURCES: Final[Mapping[str, tuple[str, ...]]] = {
    "service": ("slots", "context"),        # 用户说了服务名 / 上下文里有目标实体
    "cluster": ("slots", "config"),
    "namespace": ("slots",),
    "window": ("slots", "config"),
    "symptom": ("slots", "context"),
    "start": ("context",),
    "end": ("context",),
    "query": ("generated",),                # 查询语句由 Agent 生成，**不是**用户槽位
    "action_class": ("generated",),
    "target": ("slots", "board"),
    "evidence_id": ("board",),
    "change_id": ("user",),                 # ★ 变更单号只能用户给（无从猜）
}

#: 只能由用户提供的字段（`FIELD_SOURCES` 里 source 含 `user` 的字段，导入期一致）
USER_ONLY_FIELDS: Final[frozenset[str]] = frozenset(
    name for name, sources in FIELD_SOURCES.items() if "user" in sources
)

#: 工具 → 它取的是哪一路信号（进 `EvidencePlan.signals` 与 `Evidence.source`）
TOOL_SIGNALS: Final[Mapping[str, str]] = {
    "promql_read": EvidenceSource.METRICS.value,
    "logql_read": EvidenceSource.LOGS.value,
    "traceql_read": EvidenceSource.TRACES.value,
    "change_read": EvidenceSource.CHANGES.value,
    "topology_read": EvidenceSource.TOPOLOGY.value,
    "memory_read": EvidenceSource.MEMORY.value,
}

#: 这一路查完能**排除**什么假设（计划必须说清"能排除什么"）
TOOL_EXCLUSIONS: Final[Mapping[str, str]] = {
    "promql_read": "排除该服务自身指标劣化（资源/延迟/连接池全程平稳时）",
    "logql_read": "排除应用层报错爆发（窗口内无 ERROR 聚类时）",
    "traceql_read": "排除调用链耗时瓶颈（span 耗时分布正常时）",
    "change_read": "排除变更触发（窗口内无变更记录时）",
    "topology_read": "排除邻居传播（上下游依赖边均正常时）",
    "memory_read": "排除历史同类故障复现（无相似案例时）",
}

#: 用户只可能从"我做了什么变更"这条线索里给出的信息 → 缺口 → 一句人话问题
GAP_QUESTIONS: Final[Mapping[str, str]] = {
    "change_id": "这次故障前后有没有对应的变更单号（形如 CHG-1024）？有的话我可以直接定位那次变更。",
}


# ======================================================================
#  ② 计划里的元素
# ======================================================================
@dataclass(frozen=True)
class PlannedCall:
    """一次**通过可填充性检查**、可以真的发出去的调用。"""

    role: str
    tool: str
    params: dict[str, Any]
    signal: str
    rationale: str
    purpose: str = "evidence"            # `evidence` / `aux`（QPS 这类辅助调用）
    filled_from: dict[str, str] = field(default_factory=dict)
    query_spec: Any = None
    batch_services: tuple[str,...] = ()
    batch_index: int = 0
    evidence_source: str = ''

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "tool": self.tool, "params": dict(self.params),
                "signal": self.signal, "rationale": self.rationale,
                "purpose": self.purpose, "filled_from": dict(self.filled_from),
                'batch_services':list(self.batch_services),'batch_index':self.batch_index,'evidence_source':self.evidence_source,
                **({'query_spec':self.query_spec.summary()} if self.query_spec else {})}


@dataclass(frozen=True)
class PendingGap:
    """缺一个**只能由用户提供**的必填参数（去重后的一条待决项）。

    ★ `field` 是去重键（不是 `question` 文本）：两条问法不同的措辞也算同一个缺口。
    """

    field: str
    question: str
    tools: tuple[str, ...] = ()         # 哪些工具因为缺它而**没有**被调用
    roles: tuple[str, ...] = ()
    source: str = "user"

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "question": self.question,
                "tools": list(self.tools), "roles": list(self.roles),
                "source": self.source}


@dataclass
class PlanOutcome:
    """规划结果：**一份契约计划 + 三种处置的去向**。

    ★ `plan` 是契约对象（`targets/rationale/expected_exclusions/signals`），
      另外三个字段是**执行侧**的落点：哪些调用发得出去、哪些等用户、哪些路缺信号。

    ★ `degraded` 的 `signal` 有两种粒度，**两种都是"这一路缺失"的准确说法**：
      · 整个角色一条调用都没规划出来 → 数据源级（`metrics` / `logs` …）；
      · 某一条调用被跳过（缺必填/参数不合 schema）→ 细信号级（`pool_waiting` …）。
    """

    plan: EvidencePlan
    calls: list[PlannedCall] = field(default_factory=list)
    gaps: list[PendingGap] = field(default_factory=list)
    degraded: list[tuple[str, str]] = field(default_factory=list)   # (signal, reason)
    #: ★ **源级**的缺失信号（取值闭集 = `EvidenceSource`），供 `AgentState.degraded_signals`
    #: 与"对用户可见的缺失信号源"使用；细信号名只留在 `degraded` 的文本里。
    degraded_sources: list[str] = field(default_factory=list)
    deferred: list[PlannedCall] = field(default_factory=list)

    @property
    def unresolved(self) -> list[str]:
        """回写给编排层的未满足项 —— **缺口字段名，已去重**。"""
        return [gap.field for gap in self.gaps]

    @property
    def planned_tools(self) -> list[str]:
        return [call.tool for call in self.calls]

    def to_dict(self) -> dict[str, Any]:
        return {"plan": self.plan.model_dump(mode="json"),
                "calls": [call.to_dict() for call in self.calls],
                "budget_unqueried": [call.to_dict() for call in self.deferred],
                "gaps": [gap.to_dict() for gap in self.gaps],
                "degraded": [{"signal": s, "reason": r} for s, r in self.degraded],
                "unresolved": self.unresolved}


# ======================================================================
#  ③ 规划器
# ======================================================================
class EvidencePlanner:
    """遍历 `ToolSpec.input_schema` 做静态可填充性检查，产出 `EvidencePlan`。

    Args:
        spec_lookup: 取工具声明（默认 `ToolExecutor.spec_of`）；
            ★ 可注入是**故意**的 —— 单测要能构造"某个工具确实要求 `change_id`"
            这种契约里目前不存在的形状。
        user_only_fields: 覆盖"只能由用户提供"的字段集（默认 `USER_ONLY_FIELDS`）。
        asked: 已经问过的缺口字段 —— **这里不产生重复提问**（编排层传入会话记忆里的记账）。
    """

    def __init__(self, spec_lookup: Callable[[str], Optional[Any]], *,
                 user_only_fields: Optional[Iterable[str]] = None,
                 asked: Iterable[str] = (), registry=None) -> None:
        self._spec_lookup = spec_lookup
        self.user_only = frozenset(user_only_fields or USER_ONLY_FIELDS)
        self.asked = frozenset(asked)
        self.registry=registry

    # ------------------------------------------------------------------
    def plan(self,ctx:AgentContext,agents:Sequence[ForensicsAgent],*,asked:Iterable[str]=())->PlanOutcome:
        from dataclasses import replace
        from .batches import contexts
        parts=[]
        for agent in sorted(agents,key=lambda a:a.role):
            for index,batch in enumerate(contexts(ctx,getattr(agent,'batch_limit',5))):
                part=self._plan_single(batch,[agent],asked=asked)
                part.calls=[replace(c,batch_services=(str(c.params['service']),) if c.params.get('service') else tuple(batch.services),batch_index=index) for c in part.calls]
                parts.append(part)
        calls=[c for p in parts for c in p.calls]
        gaps={g.field:g for p in parts for g in p.gaps}
        plan=EvidencePlan(targets=self._targets(ctx),rationale=self._rationale(ctx,agents,calls,gaps),
            expected_exclusions=self._exclusions(calls),signals=sorted({s for p in parts for s in p.plan.signals}))
        return PlanOutcome(plan=plan,calls=calls,gaps=list(gaps.values()),
            degraded=list(dict.fromkeys(v for p in parts for v in p.degraded)),
            degraded_sources=list(dict.fromkeys(s for p in parts for s in p.degraded_sources)))

    def _plan_single(self, ctx: AgentContext, agents: Sequence[ForensicsAgent], *,
             asked: Iterable[str] = ()) -> PlanOutcome:
        """产出计划。

        ★ `asked` 是**这一次规划**的事实（从会话记忆里取"已经问过什么"），
          **不是规划器的属性**：以前 runner 直接改写
          `self.planner.asked`，于是复用一个 runner 处理并发事件时，
          "A 事件问过的缺口"会泄漏到 B 事件上，把 B 本该提的澄清压掉。
        """
        already_asked = frozenset(self.asked) | frozenset(asked)
        calls: list[PlannedCall] = []
        gaps: dict[str, PendingGap] = {}          # ★ 按 field 去重（不是按 Agent）
        degraded: list[tuple[str, str]] = []
        degraded_sources: list[str] = []
        from ..queries.capabilities import view
        query_tools=view(self.registry) if self.registry else {}

        def mark_degraded(signal: str, reason: str, source: str) -> None:
            """记一条"这一路没查成"：**文本给细信号**，**源级给闭集**。"""
            degraded.append((signal, reason))
            if source:
                degraded_sources.append(source)

        # ★ 排序只为**可复现**（并行取证的顺序不该影响计划内容）
        for agent in sorted(agents, key=lambda a: a.role):
            fallback = TOOL_SIGNALS.get(agent.tools[0], agent.source.value) if agent.tools else ""
            requests = list(agent.plan_calls(ctx))
            if not requests:
                # ★ 一个角色**一条调用都没规划出来**也必须留痕：
                #   否则黑板上只会看到"4 路有结果"，看不出第 5 路是被跳过的（沉默降级）
                mark_degraded(
                    fallback or agent.role,
                    f"{agent.role} 没有产出任何可执行的调用（槽位/上下文里缺少它能用的入参）"
                    f"—— 这一路**没查**，不是「查了没异常」",
                    TOOL_SIGNALS.get(agent.tools[0], agent.source.value) if agent.tools else "")
                continue
            for request in requests:
                # ★ 信号以**这一次调用自己声明的**为准：一个 Agent 可能查好几路
                #   （Metrics 查连接池/延迟/错误率，Trace 查 server/client）——
                #   用角色级的默认信号会把它们的区分度抹平（全部退化成 "metrics"）
                signal = request.signal or fallback
                source = TOOL_SIGNALS.get(request.tool, agent.source.value)
                self._check_isolation(agent, request)
                definition = self._spec_lookup(request.tool)
                if definition is None:
                    mark_degraded(signal or request.tool,
                                  f"工具 {request.tool} 未注册 —— 这一路取不到数据", source)
                    continue
                decided = self._fill(ctx, agent, request, definition)
                if decided["missing_user"]:
                    field_name = decided["missing_user"]
                    self._record_gap(gaps, field_name, agent, request, already_asked)
                    continue
                if decided["missing_hard"]:
                    mark_degraded(
                        signal or request.tool,
                        f"{agent.role} 缺必填参数 {decided['missing_hard']}，"
                        f"且该字段无人能提供（不是用户能给的信息）—— 这一路跳过，"
                        f"**不硬调一次去吃 TOOL_INVALID_PARAMS**", source)
                    continue
                errors = validate_params(definition.spec.input_schema, decided["params"])
                if errors:
                    mark_degraded(
                        signal or request.tool,
                        f"{agent.role} 的调用参数过不了 {request.tool} 的 schema："
                        f"{'；'.join(errors[:3])} —— 这一路跳过（参数是本地拼的，属于代码问题）",
                        source)
                    continue
                from ..tools.availability import check_availability
                ready=check_availability(getattr(definition,'availability',None),decided['params'])
                if not ready.ready:
                    mark_degraded(signal or request.tool,ready.reason+'；'+ready.remedy,source)
                    continue
                try:
                    descriptor=query_tools.get(request.tool)
                    if descriptor and agent.role not in descriptor.roles:raise PermissionError('role cannot plan this query capability')
                    from ..queries.binding import for_request
                    query_spec=for_request(ctx,request,definition,decided['params'])
                except (ValueError,PermissionError) as exc:
                    mark_degraded(signal or request.tool,'查询计划不能编译：'+str(exc),source)
                    continue
                calls.append(PlannedCall(
                    role=agent.role, tool=request.tool, params=decided["params"],
                    signal=signal or request.tool, rationale=request.rationale,
                    purpose=request.purpose, filled_from=decided["filled_from"],query_spec=query_spec,evidence_source=source))
                if request.dropped:
                    # ★★ "有界地少查了一部分"必须留痕（见 `ToolRequest.dropped`）：
                    #   少查 ≠ 没异常，而计划里若不写这一条，只看得到"查过的那些"。
                    mark_degraded(signal or request.tool, request.dropped, source)

        plan = EvidencePlan(
            targets=self._targets(ctx),
            rationale=self._rationale(ctx, agents, calls, gaps),
            expected_exclusions=self._exclusions(calls),
            # ★ 这里要的是**数据源级**的信号（契约闭集：metrics/logs/traces/...），
            #   而不是细信号名（pool_waiting / trace_duration_server …）。
            #   两者是不同粒度：`Evidence.source` 是闭集，`Evidence.signal` 是细路。
            #   QPS 是辅助调用，它不是一路"证据信号"。
            signals=sorted({TOOL_SIGNALS.get(call.tool, "")
                            for call in calls if call.purpose == "evidence"} - {""}),
        )
        return PlanOutcome(plan=plan, calls=calls,
                           gaps=list(gaps.values()), degraded=degraded,
                           degraded_sources=list(dict.fromkeys(degraded_sources)))

    # ------------------------------------------------------------------
    #  可填充性检查本体
    # ------------------------------------------------------------------
    def _fill(self, ctx: AgentContext, agent: ForensicsAgent, request: ToolRequest,
              definition: Any) -> dict[str, Any]:
        """逐个补齐 `required` 字段，返回 {params, filled_from, missing_user, missing_hard}。

        `definition` 是 `ToolDefinition`（声明 + 实现 + 策略），入参 schema 在
        `definition.spec.input_schema` 上 —— 这里读的就是**第 1 道保护用的那份 schema**，
        不是另抄一份（抄一份就会出现"规划时能填、执行时被拒"）。
        """
        params = dict(request.params)
        filled_from = {name: "agent" for name, value in params.items()
                       if value not in (None, "", [], {})}
        missing_user = ""
        missing_hard = ""

        for name in definition.spec.input_schema.get("required") or []:
            if params.get(name) not in (None, "", [], {}):
                continue
            sources = FIELD_SOURCES.get(name)
            if sources is None:
                # ★ 没登记的字段：**当 bug 对待**（fail-closed，不猜、不硬调）
                missing_hard = missing_hard or f"{name}（未登记来源）"
                continue
            value = None
            for source in sources:
                if source in ("user", "generated"):
                    continue
                value = self._fill_from(source, name, ctx)
                if value is not None and value != "":
                    filled_from[name] = source
                    break
            if value is not None and value != "":
                params[name] = value
                continue
            if "user" in sources and name in self.user_only:
                missing_user = missing_user or name
            else:
                missing_hard = missing_hard or name

        return {"params": params, "filled_from": filled_from,
                "missing_user": missing_user, "missing_hard": missing_hard}

    @staticmethod
    def _fill_from(source: str, name: str, ctx: AgentContext) -> Any:
        """从某一类来源里取值（取不到就 None —— **不编造**）。"""
        slots = ctx.slots
        if source == "slots":
            if name == "service":
                # ★★ **多服务范围不许静默收窄成一个**：`service_names` 里有几个服务
                #   就说明"用户要一起看这几个"，从中挑第 0 个等于把范围改成第 0 个
                #   （而清单的第 0 个固定是 gateway）→ 问全局却只查一个。
                #   所以：只有一个服务时才回填；多个服务时**回 None**（fail-closed），
                #   由取证 Agent 自己生成 `service=~"a|b|c"` 覆盖全部目标服务。
                names = [str(item) for item in slots.service_names if item]
                if len(names) == 1:
                    return names[0]
                return None
            if name in ("cluster", "namespace"):
                return getattr(slots, name, None)
            if name == "window":
                return slots.time_window
            if name == "symptom":
                return slots.symptom
            return None
        if source == "context":
            if name == "service":
                entity = ctx.entity()
                return entity.name if entity else None
            if name == "start":
                return ctx.window_start.isoformat()
            if name == "end":
                return ctx.window_end.isoformat()
            if name == "symptom":
                return slots.symptom
            return None
        if source == "config":
            if name == "cluster":
                # ★ 集群名**不发明默认值**：配置/环境没给就是空（identity 阶梯在
                #   `common/identity.py`，它已经把"用户明说 → 自报 → 环境 → 空"串好了）。
                #   契约允许省略 `@cluster`，所以"空"是可用的。
                return slots.cluster or None
            if name == "window":
                return slots.time_window or ACTIVE_SLOTS.default_time_window
            return None
        if source == "board":
            # 黑板来源的字段（`evidence_id`）只有后置角色才可能填；
            # 取证阶段填不出来是**正常**的，不编造。
            return None
        return None

    def _record_gap(self, gaps: dict[str, PendingGap], name: str,
                    agent: ForensicsAgent, request: ToolRequest,
                    asked: frozenset[str] = frozenset()) -> None:
        """记一条待决项；**同一字段只有一条**。

        `asked` 由 `plan()` 传进来（那一次规划的事实），不看实例状态。
        """
        if name in asked:
            # 已经问过了（编排层的记账）：不再重复问，也不新建待决项
            return
        existing = gaps.get(name)
        if existing is not None:
            if request.tool not in existing.tools:
                gaps[name] = PendingGap(
                    field=name, question=existing.question,
                    tools=existing.tools + (request.tool,),
                    roles=existing.roles + (agent.role,), source=existing.source)
            return
        question = GAP_QUESTIONS.get(
            name, f"能提供 {name} 吗？这个参数只能由你给出，数据源里查不到。")
        gaps[name] = PendingGap(field=name, question=question,
                                tools=(request.tool,), roles=(agent.role,))

    @staticmethod
    def _check_isolation(agent: ForensicsAgent, request: ToolRequest) -> None:
        """★ 隔离违规在**规划期**就炸：取证 Agent 只能声明自己的工具。"""
        if request.tool not in agent.tools:
            raise PermissionError(
                f"{agent.role} 试图声明 {request.tool}，但它只持有 {agent.tools} —— "
                f"证据隔离不允许一个取证 Agent 借用别人的数据源")

    # ------------------------------------------------------------------
    #  计划的可解释部分
    # ------------------------------------------------------------------
    @staticmethod
    def _targets(ctx: AgentContext) -> list[str]:
        targets = [ctx.entity().id] if ctx.entity() else []
        for name in ctx.services:
            entity = ctx.entity(name)
            if entity and entity.id not in targets:
                targets.append(entity.id)
        return targets

    @staticmethod
    def _rationale(ctx: AgentContext, agents: Sequence[ForensicsAgent],
                   calls: Sequence[PlannedCall], gaps: Mapping[str, PendingGap]) -> str:
        who = "、".join(ctx.services) or "（未指定服务）"
        parts = [
            f"针对 {who} 在 [{ctx.window_start.isoformat()}, {ctx.window_end.isoformat()}] "
            f"窗口内并行取 {len(calls)} 路信号（计划 {len(agents)} 路）"
        ]
        if ctx.slots.symptom:
            parts.append(f"现象：{ctx.slots.symptom}")
        if gaps:
            parts.append(f"另有 {len(gaps)} 个缺口只能由用户提供："
                         f"{'、'.join(sorted(gaps))} —— 已回写 unresolved，本次不猜")
        return "；".join(parts)

    @staticmethod
    def _exclusions(calls: Sequence[PlannedCall]) -> list[str]:
        out: list[str] = []
        for call in calls:
            text = TOOL_EXCLUSIONS.get(call.tool)
            if text and text not in out:
                out.append(text)
        return out


# 导入期一致性：`USER_ONLY_FIELDS` 必须与 `FIELD_SOURCES` 完全对得上。
# ★ 用 `raise` 而不是 `assert`：`assert` 在 `python -O` 下会被删掉，
#   而这条检查要守的是"可填充性检查的依据表自身不能自相矛盾"（红线需单元测试）。
if USER_ONLY_FIELDS != {name for name, sources in FIELD_SOURCES.items()
                        if "user" in sources}:
    raise ValueError(
        "USER_ONLY_FIELDS 与 FIELD_SOURCES 的 `user` 来源不一致 —— "
        "两张表描述的是同一件事，不一致就会出现「表说没人能给、检查器却当用户能给」")
