"""取证 Agent 的公共骨架。

| 类 | 作用 |
|---|---|
| `AgentContext` | 一个取证 Agent **能看到的一切**（刻意很小：槽位 + 时间窗 + incident_id） |
| `ToolRequest` | Agent 声明"我想查什么"（工具 + 参数 + 理由 + 期望信号） |
| `AgentOutcome` | 一次取证的产物（证据 / 查了没异常 / 没查成 / 要问用户的缺口） |
| `ForensicsAgent` | 抽象基类：**只允许通过 `ToolExecutor` 调自己的工具** |
| `EvidenceDraft` | 从工具结果构造 `Evidence` 的中间形态（便于单测与复用） |

--------------------------------------------------------------------------------
★★ 证据隔离怎么在代码上落地
--------------------------------------------------------------------------------

| 隔离手段 | 实现 |
|---|---|
| 各自只持有**自己的**工具 | `ForensicsAgent.tools`（本类）+ `agents.AGENT_TO_TOOLS`（与 `AGENT_TOOL_MATRIX` 一致，导入期断言） |
| 看不到别人的上下文 | `AgentContext` **只有**槽位/时间窗/incident_id —— 没有别的 Agent 的输出、没有黑板句柄、没有自然语言对话 |
| 看不到别人的证据 | 黑板 `read_evidence` 对取证角色直接拒绝（`BoardPermissionError`） |
| 传递的只有结构化证据 | `AgentOutcome.evidence` 是 `list[Evidence]`（**不是**自然语言） |

★ "看不到"必须**是类型/接口层面的事实**，而不是"大家自觉不调"：
  所以 `AgentContext` 里**根本没有**能通向别人数据的东西 —— 想违反也无从下手。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Final, Mapping, Optional, Sequence

from ..common.contracts import EntityRef, Evidence, EvidencePlan, Slots
from ..common.steps import shrink_params
from ..common.enums import EvidenceDirection, EvidenceSource,AGENT_TOOL_MATRIX
from ..board.store import new_evidence_id
from ..tools.executor import ToolExecutor

__all__ = [
    "AgentContext",
    "ToolRequest",
    "AgentOutcome",
    "ForensicsAgent",
    "AGENT_TO_TOOLS",
    "MAX_TARGET_SERVICES",
    "target_services",
    "dropped_services",
    "cap_note",
    "evidence_from_result",
    "to_datetime",
]

logger = logging.getLogger("agent.agents.base")

#: Agent 角色 → 它**能用的工具**（与 `enums.AGENT_TOOL_MATRIX` 必须一致，导入期断言）。
#: ★ 取证 Agent 的**主工具**排在第一个（`planner` 用它取"这一路的数据源"）——
#:   后面的工具都是**同一条证据链上的补充手段**（如 `raw_read` 回取原文），
#:   不引入第二路证据来源。
AGENT_TO_TOOLS: Final[Mapping[str, tuple[str, ...]]] = {
    **{r:AGENT_TOOL_MATRIX[r] for r in ('database_agent','runtime_agent','lifecycle_agent','sdk_access_agent')},
    "metrics_agent": ("promql_read", "raw_read"),
    "log_agent": ("logql_read",),
    "trace_agent": ("traceql_read", "trace_read"),
    "change_agent": ("change_read",),
    "topology_agent": ("topology_read",),
}

#: 一次排查最多同时盯几个服务（服务多了，单条查询的返回量会爆）。
#: ★ 默认值只有这一处；各 Agent 要用更小的上限（拓扑/变更查询更重）时**显式传**
#:   `limit=`，不要各写一份自己的数字。
MAX_TARGET_SERVICES: Final[int] = 5


def target_services(ctx: "AgentContext", *, limit: int = MAX_TARGET_SERVICES) -> list[str]:
    """这一步要对哪几个服务发查询（**5 路取证共用的唯一实现**）。

    槽位里给了几个服务名就查几个，再加上本次的目标实体（它可能不在服务名列表里），
    按出现顺序去重、并按 `limit` 截断。

    ★ 为什么放在基类：五个 Agent 各写一份逐字相同的循环时，"上限是几"会散成五个数字，
      改一个漏一个就会出现"指标查 5 个、拓扑查 3 个"这种**没人解释得了的口径差异**。
    """
    names: list[str] = []
    for name in list(ctx.services) + ([ctx.entity().name] if ctx.entity() else []):
        if name and name not in names:
            names.append(str(name))
    return names[:max(0, int(limit))]


def dropped_services(ctx: "AgentContext", picked: Sequence[str],
                     *, limit: int = MAX_TARGET_SERVICES) -> list[str]:
    """因为上限而**没被查**的服务（要进 `degraded`：截断不是"本来就没有"）。

    ★★ 这条留痕是"范围事实"的一半：用户问了 6 个服务、我们只查了 5 个，
      答案里必须看得到少了哪一个 —— 否则"没查到异常"会被读成"它没问题"。
    """
    wanted: list[str] = []
    for name in list(ctx.services) + ([ctx.entity().name] if ctx.entity() else []):
        if name and str(name) not in wanted:
            wanted.append(str(name))
    return [name for name in wanted[len(picked):] if name not in picked]


#: 截断留痕的唯一文案（五个 Agent 共用；散成五份迟早有一份漏写）。
TRUNCATION_NOTE: Final[str] = "★ 超出单轮服务上限 {limit} 个，本路**没有查**：{names}"


def cap_note(ctx: "AgentContext", picked: Sequence[str], *,
             limit: int = MAX_TARGET_SERVICES) -> str:
    """给 `degraded` 用的截断说明（没截断就返回空串）。"""
    dropped = dropped_services(ctx, picked, limit=limit)
    if not dropped:
        return ""
    return TRUNCATION_NOTE.format(limit=limit, names="、".join(dropped))


#: 判定"这个数字是纳秒"的门槛。★★ 必须是 **1e16**：
#:   以前的 `1e12` 会把 **epoch 毫秒**（今天的值 ≈1.7e12）误判成纳秒 → 除以 1e9
#:   之后落到 1970 年，随后 `_clamp_onset` 又把它**静默**改成窗口起点 ——
#:   于是"证据的起始时刻"变成了一个我们编出来的值（比报错坏得多）。
#:   真实的纳秒时间戳在 1e18 量级，1e16 与它、与毫秒/微秒都有足够安全距离。
_NANOSECOND_THRESHOLD: Final[int] = 10 ** 16


def to_datetime(value: Any, *, default: Optional[datetime] = None) -> datetime:
    """把 epoch 秒 / 毫秒 / 纳秒 / ISO 串 / datetime 统一成 aware datetime。

    ★ 只有越过 `_NANOSECOND_THRESHOLD`（1e16，≈纳秒量级）才按纳秒换算；
      毫秒/微秒会被当成"秒"读成一个明显荒谬的时间（远超 now），
      由调用方的窗口校验拦下 —— **宁可报错，也不要静默改写时间**。
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default or datetime.now(timezone.utc)
        if text.isdigit():                     # 纳秒 or 秒：按数量级判断
            number = int(text)
            seconds = (number / 1e9 if number > _NANOSECOND_THRESHOLD
                       else float(number))
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return default or datetime.now(timezone.utc)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        number = float(value)
        seconds = number / 1e9 if number > _NANOSECOND_THRESHOLD else number
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    return default or datetime.now(timezone.utc)


# ======================================================================
@dataclass(frozen=True)
class AgentContext:
    """取证 Agent 的**全部**可见输入。

    ★ 这里没有的东西才是重点：没有别的 Agent 的结论、没有黑板、没有对话历史。
      （隔离硬约束：每个取证 Agent 只看得见"槽位 + 时间窗"。）
    """

    incident_id: str
    slots: Slots
    window_start: datetime
    window_end: datetime
    tenant_id: str = "default"
    query_scope: Any = None
    # Source declarations only, never another agent's observations or conclusions.
    metric_catalog: Optional[tuple] = None
    metric_services: tuple = ()

    @property
    def services(self) -> tuple[str, ...]:
        return tuple(self.slots.service_names)

    def entity(self, name: Optional[str] = None) -> Optional[EntityRef]:
        """目标实体：优先槽位里的，其次第一个服务名。

        ★ 集群名**照抄槽位**（不再发明默认值）：槽位里的集群来自身份阶梯
          （用户明说 → 被观测系统自报 → 环境变量 → 空），空就是空 ——
          契约允许 `svc:order` 这种省略集群名的形式。
        """
        if self.slots.service is not None and name is None:
            return self.slots.service
        target = name or (self.services[0] if self.services else None)
        if not target:
            return None
        return EntityRef.make("service", target, self.slots.cluster, self.slots.namespace)

    def query_window(self) -> dict[str, Any]:
        """给工具用的时间窗参数（工具自己把它归一成 epoch）。"""
        return {"start": self.window_start.isoformat(), "end": self.window_end.isoformat()}

    @property
    def window_seconds(self) -> float:
        """本次窗口有多长（秒）—— ★ 取数预算（step 放大 / 截断）用它算。

        ★ 负值一律夹成 0：那是配置或调用方给错了，取数层不该据此去放大 step
          （`plan_step` 会退回基础步长）。
        """
        return max(0.0, (self.window_end - self.window_start).total_seconds())

    def to_dict(self) -> dict[str, Any]:
        return {"incident_id": self.incident_id,
                "window": [self.window_start.isoformat(), self.window_end.isoformat()],
                "services": list(self.services)}


@dataclass(frozen=True)
class ToolRequest:
    """一个 Agent"想调什么"的声明（**先声明、后校验、再执行**）。

    `purpose` 区分两类调用，因为它们的**结局语义不同**：

    | purpose | 例子 | 查到空结果意味着 |
    |---|---|---|
    | `evidence` | `pool_waiting` 是否上涨 | "查了没异常" → 记 `signal_checked` |
    | `aux` | 查 QPS（只是给 RCA 折算影响面） | 与"有没有异常"无关 → **不能**记成"这一路没异常" |
    """

    tool: str
    params: dict[str, Any] = field(default_factory=dict)
    signal: str = ""                     # 这次调用取的是哪一路信号（进 timeline / EvidenceSource）
    rationale: str = ""                  # "为什么查它"（进 EvidencePlan）
    purpose: str = "evidence"            # `evidence` / `aux`
    #: ★★ 这次调用**明知少查了一部分**时的说明（如"超过单轮服务上限，没查 X、Y"）。
    #:   它进计划的 `degraded` —— 有界不是问题，**静默**的有界才是：
    #:   用户问 6 个服务、我们只查 5 个，"没查到异常"会被读成"它没问题"。
    dropped: str = ""
    #: ★★ 这次调用的**唯一编号**（同一次排查内唯一）：SSE 的 `tool` 事件靠它把
    #:   `phase="done"` 贴回对应的 `phase="start"` —— 并发调用不能靠"同名 FIFO"配对。
    call_id: str = ""
    query_spec: Any = None
    retain_aux_result: bool = False

    @property
    def is_evidence(self) -> bool:
        return self.purpose == "evidence"

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "params": dict(self.params),
                "signal": self.signal, "rationale": self.rationale,
                "purpose": self.purpose, "call_id": self.call_id,
                "dropped": self.dropped}


@dataclass
class AgentOutcome:
    """一次取证的产物。

            ★ 四类结果**必须分开**（三分法 + 缺口）：
      · `evidence`：查到了异常 → 结构化证据
      · `empty`：**查了，没异常**（这是"排除假设"的依据，不是"没有线索"）
      · `degraded`：**没查成**（数据源不可用/超时/查询被拒）→ 这一路信号缺失
      · `pending`：缺的是**只能由用户提供**的参数 → 交给编排层决定是否澄清
    """

    role: str
    evidence: list[Evidence] = field(default_factory=list)
    empty: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    #: ★ **源级**的缺失信号（`metrics`/`logs`/…，取值闭集来自 `EvidenceSource`）。
    #: 与 `degraded`（细信号 + 原因，给人看）分开：`AgentState.degraded_signals`
    #: 只接受闭集取值，细信号名（如 `pool_waiting`）进那个字段会**直接抛错**
    #: —— 那正好是这个机制该生效的时候崩。
    degraded_sources: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    aux: list[str] = field(default_factory=list)
    qps_estimates: dict[str, float] = field(default_factory=dict)
    #: 成功调用的**原始返回值**（按 `request.signal` 索引），只给 `combine()` 用；
    #: 不进 `to_dict()`（它可能很大，而且不是给人看的留痕）
    results: dict[str, Any] = field(default_factory=dict, repr=False)

    def merge(self, other: "AgentOutcome") -> "AgentOutcome":
        """把同一角色多次调用的产物并成一份（顺序稳定，便于断言）。"""
        if other.role != self.role:
            raise ValueError(f"不能合并不同角色的产物：{self.role} vs {other.role}")
        self.evidence.extend(other.evidence)
        for name in ("empty", "degraded", "degraded_sources", "pending", "calls", "aux"):
            getattr(self, name).extend(getattr(other, name))
        self.qps_estimates.update(other.qps_estimates)
        self.results.update(other.results)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "evidence": len(self.evidence), "empty": list(self.empty),
                "degraded": list(self.degraded),
                "degraded_sources": list(self.degraded_sources),
                "pending": list(self.pending),
                "calls": list(self.calls), "aux": list(self.aux),
                "qps": dict(self.qps_estimates)}


# ======================================================================
class ForensicsAgent(ABC):
    """取证 Agent 基类。

    ★ 子类**只能**通过 `executor` 调 `self.tools` 里列出的工具；
      executor 的第 2 道保护（能力交集）会再兜一层 —— 两层都拦得住才算真的隔离。
    """

    role: str = ""
    source: EvidenceSource = EvidenceSource.METRICS
    tools: tuple[str, ...] = ()

    def __init__(self) -> None:
        if not self.role:
            raise ValueError("ForensicsAgent 子类必须声明 role")
        expected = AGENT_TO_TOOLS.get(self.role)
        if expected is not None and tuple(self.tools) != tuple(expected):
            raise ValueError(
                f"{self.role} 的工具集 {self.tools} 与 AGENT_TO_TOOLS 的 {expected} 不一致 —— "
                f"证据隔离的前提是「每个 Agent 只持有自己的工具」")
        if not self.tools:
            raise ValueError(
                f"{self.role} 应当至少持有 1 个工具（按角色与工具矩阵），实际 {self.tools}")

    # ------------------------------------------------------------------
    @abstractmethod
    def plan_calls(self, ctx: AgentContext) -> list[ToolRequest]:
        """声明这次要查什么（**不执行**）—— 规划器先做可填充性检查。"""

    @abstractmethod
    def build_evidence(self, ctx: AgentContext, request: ToolRequest,
                       result: Any) -> list[Evidence]:
        """把一次成功的工具结果转成结构化证据（`onset` 必须落在窗口内）。"""

    # ------------------------------------------------------------------
    def summarize_empty(self, request: ToolRequest, result: Any) -> str:
        """这一路"查了没异常"时的信号名（默认用 request.signal）。"""
        return request.signal or request.tool

    def qps_from(self, ctx: AgentContext, request: ToolRequest,
                 result: Any) -> dict[str, float]:
        """辅助型调用（`purpose="aux"`）的产物 → 黑板上的结构化数字。

        ★ 默认什么都不产：只有 Metrics Agent 会覆盖它（查 QPS 写 `qps_estimates`）。
        """
        return {}

    def followup_requests(self, ctx: AgentContext, outcome: AgentOutcome,
                          hints: Sequence[str] = ()) -> list[ToolRequest]:
        """本轮拿到结果之后，**还要不要按需回取**（默认：不回取）。

        Args:
            hints: ★★ 运行器转交的**跨角色结构化线索**（当前只用到 `trace_id`：
                它长在日志那一路的证据里）。取证 Agent 之间看不到彼此的证据，
                所以这类线索只能由运行器按同一次 run 的事实转交。
                ★ 子类**覆盖这个方法时必须接住它** —— 运行器一律带上它，
                  签名不匹配会抛 `TypeError`，而"补充取数失败只记 aux"的兜底
                  会把它**静默**吞掉（连 `raw_read` 一起不发）。

        ★ 为什么需要这个钩子：有些补充手段的入参**只有跑完本轮才知道**
          （最典型的是 `raw_read` 要的 `raw_ref` —— 它是查询结果本身带出来的）。
          写进 `plan_calls` 是不可能的（那时还没有引用），所以留给跑完这一轮再说。
        ★ 它**不产生证据**：补充手段的结局按 `aux` 处理，不许混进"这一路没异常"。
        """
        return []

    # ------------------------------------------------------------------
    async def run_call(self, ctx: AgentContext, request: ToolRequest,
                       executor: ToolExecutor, *,
                       on_event: Optional[Callable[[str, dict[str, Any]], None]] = None
                       ) -> AgentOutcome:
        """执行**一次**已经通过可填充性检查的调用，并把结局分成四类。

        ★ 这里是"没查成"与"查了没异常"的**唯一分叉点**：
          `result.ok=False` → `degraded`（这一路信号缺失）；
          `result.ok=True` 但没有证据 → `empty`（这一路排除了一个假设）。
        """
        outcome = AgentOutcome(role=self.role)
        emit_tool_event(on_event, "start", _tool_payload(self.role, request))
        if request.query_spec is not None and hasattr(executor,'call_query'):
            result = await executor.call_query(self.role,request.query_spec)
        else:result = await executor.call(self.role, request.tool, dict(request.params))
        emit_tool_event(on_event, "done", _result_payload(self.role, request, result))
        outcome.calls.append(_call_record(request, result))

        if not getattr(result, "ok", False):
            _ok, reason = evidence_from_result(result)
            line = f"{request.signal or request.tool}：{reason}"
            if request.is_evidence:
                outcome.degraded.append(line)
                outcome.degraded_sources.append(self.source.value)
            else:
                # ★ 辅助调用（如 QPS）失败**不算**一路证据信号缺失：
                #   它本来就不产出证据，把它记进"缺失信号"会让 RCA 以为少了一路遥测。
                outcome.aux.append(line)
            return outcome

        if not request.is_evidence:
            if request.retain_aux_result:outcome.results[request.signal or request.tool]=result
            outcome.qps_estimates.update(self.qps_from(ctx, request, result))
            note = str(getattr(result, "summary", ""))[:80]
            outcome.aux.append(f"{request.signal or request.tool}：辅助数据已取（{note}）")
            return outcome

        outcome.results[request.signal or request.tool] = result
        evidence = self.build_evidence(ctx, request, result)
        if evidence:
            outcome.evidence.extend(evidence)
        else:
            outcome.empty.append(self.summarize_empty(request, result))
        return outcome

    def _clamp_onset(self, onset: datetime, window: tuple[datetime, datetime]) -> datetime:
        """把 onset 夹进观测区间。

        ★ 为什么需要：数据源可能返回窗口外的点（Prometheus 的 step 对齐、
          Loki 的纳秒取整都会造成几秒偏差），而 `Evidence` 的契约**强制**
          `onset` 落在 `time_range` 内 —— 越界会被契约校验器拒掉（那是好事，
          但一个几秒的取整偏差不该让整条证据作废）。
        """
        start, end = window
        if onset < start:
            return start
        if onset > end:
            return end
        return onset

    def make_evidence(self, ctx: AgentContext, *, signal: str, direction: EvidenceDirection,
                      onset: datetime, summary: str, raw_ref: str,
                      entity: Optional[EntityRef] = None, magnitude: Optional[float] = None,
                      magnitude_unit: Optional[str] = None,
                      baseline: Optional[float] = None, confidence: float = 0.8,
                      tags: Sequence[str] = (),
                      time_range: Optional[tuple[datetime, datetime]] = None) -> Evidence:
        """构造一条证据（统一入口：保证 `onset`/`raw_ref`/`time_range` 不会漏）。

        `time_range` 默认是 AgentContext 的观测窗口；**当某一路查的范围与观测窗口
        不同**时（如变更取证要往前多看 15 分钟），必须显式传入**真正查过的那个区间** ——
        否则 onset 会被夹进观测窗口，把"变更发生在异常之前 12 分钟"这个关键事实
        **改写成"变更发生在异常同一时刻"**，时序因果直接失真。
        """
        target = entity or ctx.entity()
        if target is None:
                    raise ValueError(f"{self.role}: 没有可归属的实体 —— 证据必须挂在实体上")
        window = time_range or (ctx.window_start, ctx.window_end)
        return Evidence(
            evidence_id=new_evidence_id(),
            source=self.source,
            agent_role=self.role,
            entity=target,
            signal=signal,
            direction=direction,
            magnitude=magnitude,
            magnitude_unit=magnitude_unit,
            baseline=baseline,
            time_range=window,
            onset=self._clamp_onset(onset, window),
            confidence=max(0.0, min(1.0, confidence)),
            raw_ref=raw_ref,
            summary=summary,
            tags=list(tags),
        )

    # ------------------------------------------------------------------
    def combine(self, ctx: AgentContext, results: Mapping[str, Any]) -> list[Evidence]:
        """本 Agent **多次调用之间**的汇总产物（默认没有）。

        ★ 这不是"Agent 之间通信"：`results` 只装**这个 Agent 自己**刚发出去的那几次
          调用的返回值（按 `request.signal` 索引），`AgentContext` 里依然没有
          别的 Agent 的任何东西。Trace Agent 用它做 client/server 对比。
        """
        return []


def evidence_from_result(result: Any) -> tuple[bool, str]:
    """统一的"这次调用算什么结局"判定（供各 Agent 复用）。

    Returns:
        `(ok, reason)`；`ok=False` 时 reason 直接进 `degraded`。
    """
    if getattr(result, "ok", False):
        return True, ""
    code = getattr(result, "error_code", "") or ""
    detail = getattr(result, "summary", "") or code
    if getattr(result, "degraded", False):
        return False, f"没查成（{code}）：{detail[:120]}"
    return False, f"调用失败（{code}）：{detail[:120]}"


def _shrink(params: Mapping[str, Any], *, limit: int = 200) -> dict[str, Any]:
    """进审计/留痕/事件用的参数副本（**保形 + 有界**，与事件那条**同一实现**）。"""
    return shrink_params(params, limit)


#: ★★ 工具名 → **给用户看的中文短名**（工具事件的 `title`）。
#:   为什么要中文：前端要显示「查询指标」而不是 `promql_read` —— 前者用户能读，
#:   后者留给折叠区做追溯（两个都给，不缺一个）。
TOOL_TITLES: Final[dict[str, str]] = {
    'sdk_snapshot_read':'读取当前 SDK 快照','sdk_access_read':'查询 SDK 访问历史',
    'sdk_interfaces_read':'读取 SDK 接口能力','sdk_health_read':'读取 SDK 健康信息',
    'sdk_capability_read':'检查采集能力','runtime_snapshot_read':'查询运行诊断',
    'worker_state_read':'查询工作池','connection_holders_read':'查询连接持有者',
    'process_event_read':'查询退出与重启记录','db_activity_read':'查询数据库活动',
    'db_lock_read':'查询锁等待','db_deadlock_read':'查询死锁记录',
    'db_slow_query_read':'查询慢 SQL','db_plan_read':'查询执行计划记录',
    'db_object_read':'核查数据库对象','db_context_read':'核查数据库连接目标',
    'migration_read':'查询迁移记录','ddl_audit_read':'查询数据库变更审计',
    "promql_read": "查询指标", "logql_read": "查询日志", "traceql_read": "查询链路",
    "trace_read": "读取链路明细",
    "change_read": "查询变更记录", "topology_read": "查询服务拓扑",
    "memory_read": "检索历史记忆", "evidence_lookup": "回查黑板证据",
    "kb_search": "检索知识库", "raw_read": "回取原始数据",
    "change_write": "变更动作（写）", "latency_probe": "探测延迟",
}


def tool_title(tool: str) -> str:
    """工具的中文短名（认不出来就用工具名本身 —— **不编**一个像模像样的名字）。"""
    return TOOL_TITLES.get(str(tool or ""), str(tool or "") or "工具调用")


def _tool_payload(role: str, request: ToolRequest) -> dict[str, Any]:
    """`tool` 事件里**成对复用**的那一半（参数侧）：`start` / `done` 两帧必须逐字一致。"""
    return {"call_id": request.call_id, "name": request.tool,
            "title": tool_title(request.tool), "role": role,
            "signal": request.signal, "purpose": request.purpose,
            "params": _shrink(request.params), "why": request.rationale,
            **({'query_spec':request.query_spec.summary()} if request.query_spec else {})}


def _elapsed_ms(result: Any) -> float:
    return round(float(getattr(result, "elapsed_ms", 0.0) or 0.0), 1)


def _result_payload(role: str, request: ToolRequest, result: Any) -> dict[str, Any]:
    """`done` 帧：把这次调用的**结局**如实写全（成败 / 耗时 / 结果摘要 / 回查引用）。"""
    return {**_tool_payload(role, request),
            "ok": bool(getattr(result, "ok", False)),
            "degraded": bool(getattr(result, "degraded", False)),
            "elapsed_ms": _elapsed_ms(result),
            "error_code": getattr(result, "error_code", "") or "",
            "raw_ref": getattr(result, "raw_ref", "") or "",
            "result": str(getattr(result, "summary", "") or "")[:400]}


def _call_record(request: ToolRequest, result: Any) -> dict[str, Any]:
    """进 `AgentOutcome.calls` 的留痕（审计与 `agent_done` 的耗时都读它）。"""
    return {"tool": request.tool, "signal": request.signal, "purpose": request.purpose,
            "call_id": request.call_id, "ok": bool(getattr(result, "ok", False)),
            "error_code": getattr(result, "error_code", None),
            "degraded": bool(getattr(result, "degraded", False)),
            "elapsed_ms": _elapsed_ms(result), "raw_ref": getattr(result, "raw_ref", ""),
            "params": _shrink(request.params)}


def emit_tool_event(on_event: Optional[Callable[[str, dict[str, Any]], None]],
                    phase: str, payload: Mapping[str, Any]) -> None:
    """发一条 `tool` 事件（契约修订 20）；**发不出去也不许影响排查**。

    ★★ 成对：`phase="start"`（调用前：参数 + 为什么查）/ `phase="done"`（调用后：结果）。
      硬性要求是**边跑边发** —— 等整轮结束再补一份列表就违背了"过程透明"。
    """
    if on_event is None:
        return
    try:
        on_event("tool", {"phase": phase, **dict(payload)})
    except Exception:  # noqa: BLE001 —— 事件是旁路，绝不带走排查
        logging.getLogger("agent.agents").warning("tool 事件发送失败（phase=%s）", phase,
                                                  exc_info=False)
