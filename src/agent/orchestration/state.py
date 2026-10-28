"""状态图的状态对象 + 终止判据。

--------------------------------------------------------------------------------
★ 两个对象，各管一件事
--------------------------------------------------------------------------------

| 对象 | 是什么 | 为什么不能用另一个代替 |
|---|---|---|
| `GraphState` | 图**进程内**的工作状态（还带着各类对象、留痕、计数器） | 它刻意比契约宽：契约（`AgentState`）是**对外**的状态断面，图内需要更多中间物 |
| `AgentState`（契约） | 对外的**唯一状态契约**（11 个字段） | 它是契约：字段一旦进契约，外部就依赖它。所以"图内的临时物"不许塞进去 |

`GraphState.to_agent_state()` 是两者**唯一**的转换点（并且真的过一遍 Pydantic 校验 ——
`degraded_signals` 的闭集校验就是在这里生效的）。

--------------------------------------------------------------------------------
★★ 终止判据必须**接在关键路径上**
--------------------------------------------------------------------------------

五个终止条件里，最危险的是"函数定义了但没人调用"：
图会在循环里一直跑、预算永远不生效。所以这里的 `should_terminate()`：

  1. 只在**一处**实现（不散落在各节点里）；
  2. 由 `graph.py` 的 **唯一**那条条件边上调用（`decide` 节点之后）；
  3. 单测直接断言"超预算必停 / 无增益必停" —— 这两条只要有一条没接上，测试就会红。

--------------------------------------------------------------------------------
★ 判据的求值顺序：硬停止优先于质量判据
--------------------------------------------------------------------------------

按"收敛路径"排下来是这样的，但**求值顺序**必须是：

    人工中止 / 预算耗尽  →  确认根因  →  连续两轮无增益  →  全部低分（拒答）

理由：前两个是"必须马上停"（不看证据质量），后两个是"证据质量够了/不够了"。
反过来（先判确认根因）会让"预算已耗尽但恰好有个高分假设"继续跑一轮 —— 那就是超预算。

--------------------------------------------------------------------------------
★★ 判据阈值：**唯一定义处在 `common/thresholds.py`**
--------------------------------------------------------------------------------

`CONFIRM_SCORE` / `ABSTAIN_BELOW` / `TOP2_CLOSE` 是编排与归因**共用**的三个数：
`should_terminate()` 用它判"停不停"，`attribution.arbiter` 用它判"能不能定因"。
它们只在 `common/thresholds.py` 定义一次，本模块与包再导出都只是**引用**，
**不许在别处再写一遍字面量** —— 两份数字漂移一次，终止判据与仲裁结论就会互相矛盾。
"""

from __future__ import annotations

from ..agents.base import AgentOutcome

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Optional, Sequence

from ..agents.planner import PlanOutcome  # noqa: F401
from ..common.thresholds import ABSTAIN_BELOW, CONFIRM_SCORE, TOP2_CLOSE  # noqa: F401
from ..common.contracts import (  # noqa: F401  (契约转换用)
    AgentState,
    ArbitResult,
    Evidence,
    EvidenceBoard,
    Hypothesis,
    Slots,
)
from ..common.enums import ArbitDecision, HypothesisStatus, Intent
from ..intent.router import RouteOutcome  # noqa: F401
from ..memory.session import ReferenceResolution  # noqa: F401
from .budget import Budget

__all__ = [
    "GraphState",
    "TerminationDecision",
    "should_terminate",
    "evidence_gain",
    "CONFIRM_SCORE",
    "ABSTAIN_BELOW",
    "NO_PROGRESS_GAIN",
    "TOP2_CLOSE",
    "TERMINATION_KINDS",
]

#: ──────────────────────────────────────────────────────────────────────
#: ★★ 判据阈值：**定义处只有一处** —— `common/thresholds.py`（本模块只引用）
#: ──────────────────────────────────────────────────────────────────────
#: 见模块头：三个数同时服务"停不停"与"能不能定因"两条判据，
#: `decide_node` / `orchestrator` / 包再导出一律指向同一个对象。

#: 判据原文："连续两轮无新增有效证据（似然比增益 <0.10）" —— 只服务编排，定义在此
NO_PROGRESS_GAIN: Final[float] = 0.10

TERMINATION_KINDS: Final[tuple[str, ...]] = (
    "confirmed", "no_progress", "budget", "manual", "abstain", "continue",
)


# ======================================================================
@dataclass(frozen=True)
class TerminationDecision:
    """终止判据的结论（`kind` 是机器可读的，`reason` 是给人看的）。"""

    terminate: bool
    kind: str
    reason: str
    status: str = "running"

    def to_dict(self) -> dict[str, Any]:
        return {"terminate": self.terminate, "kind": self.kind,
                "reason": self.reason, "status": self.status}


def evidence_gain(previous: Sequence[Evidence], new: Sequence[Evidence]) -> float:
    """这一轮的"新增有效证据"增益（0~1）。

    ★ 为什么不是"似然比增益"：似然比要等真公式。这里量的是**同一件事的可观测代理**：
      "这一轮新查到的有效证据占累计的比例" —— 一轮什么都没查到就是 0（"无新增有效证据"），
      一轮全是新的就是 >0。条件"连续两轮 <0.10"因此在"两轮都没查到东西"时**可达**，
      而不是拖到第 3 轮才可能触发（那样它和"轮次预算"就重复了，等于没接）。
    """
    old, fresh = len(previous), len(new)
    if fresh <= 0:
        return 0.0
    return round(fresh / float(old + fresh), 4)


def _best(hypotheses: Sequence[Hypothesis]) -> Optional[Hypothesis]:
    alive = [h for h in hypotheses if h.status != HypothesisStatus.EXCLUDED]
    return max(alive, key=lambda h: h.score, default=None)


def should_terminate(state: "GraphState", *,
                     budget_reason: Optional[str] = None) -> TerminationDecision:
    """五个终止条件（唯一实现）。**每轮取证之后必须调一次。**

    ★ 质量判据（"确认根因 / 全部低分"）由**仲裁结果**（`ArbitResult`）提供 ——
      它才是"根因到底定没定"的权威判断；本函数只负责**把它翻译成终止决定**，
      并叠加硬停止（人工/预算）与"查不动了"（连续无增益）这两类与分数无关的条件。
      这样仍然只有**一处**在回答"停不停"。
    """
    reason = budget_reason if budget_reason is not None else state.budget.exhausted()
    if reason is not None:
        kind = "manual" if state.budget.aborted else "budget"
        return TerminationDecision(
            terminate=True, kind=kind, status="budget_exhausted",
            reason=(f"{reason} → 降级输出当前最佳候选 + 不确定性说明"
                    f"（spec §3.5：预算耗尽不是失败，是**如实收口**）"),
        )

    # ---- ① 仲裁说了算 ----
    arbitration = state.arbitration
    if arbitration is not None:
        if arbitration.decision is ArbitDecision.CONCLUDED:
            root = arbitration.root_cause
            return TerminationDecision(
                terminate=True, kind="confirmed", status="concluded",
                reason=(f"仲裁：{arbitration.reason}"
                        + (f"；根因 {root.hypothesis_id}（{root.score:.2f}，"
                           f"支持证据 {len(root.supporting)} 条）" if root else "")),
            )
        if arbitration.decision is ArbitDecision.ABSTAIN:
            return TerminationDecision(
                terminate=True, kind="abstain", status="abstained",
                reason=f"仲裁：{arbitration.reason}",
            )
        # `need_targeted_evidence` → **不终止**：交给 `directed` 节点再查一轮
        # （能不能真查到由预算与 `_directed_target` 决定）

    # ---- ② 没有仲裁时（取证未跑/打分器失败）退回启发式判据 ----
    best = _best(state.hypotheses)
    if arbitration is None and best is not None and best.score >= CONFIRM_SCORE:
        return TerminationDecision(
            terminate=True, kind="confirmed", status="concluded",
            reason=(f"最高分假设 {best.hypothesis_id}={best.score:.2f} ≥ {CONFIRM_SCORE}"
                    f" 且未被否决 → 收敛（spec §3.5）"),
        )

    gains = state.evidence_gains
    if len(gains) >= 2 and gains[-1] < NO_PROGRESS_GAIN and gains[-2] < NO_PROGRESS_GAIN:
        return TerminationDecision(
            terminate=True, kind="no_progress", status="concluded",
            reason=(f"连续两轮无新增有效证据（增益 {gains[-2]:.2f} / {gains[-1]:.2f} "
                    f"均 < {NO_PROGRESS_GAIN}）→ 收敛并输出当前最佳（spec §3.5）"),
        )

    if arbitration is None and state.hypotheses and (
            best is None or best.score < ABSTAIN_BELOW):
        top = f"{best.score:.2f}" if best is not None else "（全被否决）"
        return TerminationDecision(
            terminate=True, kind="abstain", status="abstained",
            reason=(f"所有假设都低分（最高 {top} < {ABSTAIN_BELOW}）→ **拒答**并说明"
                    f"缺什么证据才能定论（spec §3.5；拒答必须给理由与建议方向）"),
        )

    return TerminationDecision(terminate=False, kind="continue", reason="继续下一轮")


# ======================================================================
#: "本次还没有先验产物"的**唯一字面量**（空快照）。
#:
#: ★★ 它承载的是一条不变量：**别把上一次的先验报成本次**。
#:   任何"这条早退路径上还没打分"的地方，都必须写出**显式等于空**的快照，
#:   而不是让渲染层去回读打分器实例上那份**上一次**的产物。
#:   三处（`_initial_flags` / 打分失败分支 / 渲染层兜底）各写一份字面量的后果实测过：
#:   漏改一处就会出现"假设是空的，却报出了上一次的 `by_category`"。
#: ★ 各处只**读**它；要写进 `flags` 时用 `dict(EMPTY_PRIOR_SNAPSHOT)` 复制一份
#:   （`flags` 里那份是**可变的**，不能被任何一处就地改写）。
EMPTY_PRIOR_SNAPSHOT: Final[dict[str, Any]] = {
    "by_category": {}, "floored_categories": [],
}


def board_store(state: "GraphState", fallback: Any = None, *,
                need: str = "") -> Any:
    """取这次运行的**黑板存储**（★ `state.board` 只是黑板**快照**，不是存储）。

    ★★ 为什么必须有这一个函数：落黑板的方法（`propose_hypotheses` /
      `raise_questions` / `publish` / `read_evidence`）长在**存储**上（`BoardStore`），
      而 `state.board` 是 `EvidenceBoard` 快照模型 —— 它上面没有这些方法。
      真机实测：有两处直接拿 `state.board` 调存储方法 → 每次都抛 `AttributeError`
      被就地吞成一句留痕，表现为**假设与缺口从来没上过黑板**，而每一份答案里
      都挂着一句"落黑板失败"。

    ★ 取法按 `flags → 传入的兜底 → state.board` 顺序，并且**按需要的方法**判断
      （鸭子类型）：只有真的长着那个方法才算数，避免又把快照当成存储用。
    """
    flags = getattr(state, "flags", None) or {}
    candidates = (flags.get("board_store"), fallback, getattr(state, "board", None))
    for candidate in candidates:
        if candidate is not None and (not need or hasattr(candidate, need)):
            return candidate
    return None


def _initial_flags() -> dict[str, Any]:
    """`GraphState.flags` 的**初始值**（不是一个空字典）。

    ★★ `prior_snapshot` 必须**从零就有**，
      且"没打分"要**显式等于空**。

      以前它只在 `_score_candidates` 里被写入，而 `_node_hypotheses` 在
      `forensics_skipped` 时会**直接 `return`**（`hypotheses_node.py`），
      `_score_candidates` 根本不跑 → `flags["prior_snapshot"]` 不存在 →
      `facts._prior_explanation` 落到"实例字段兜底" →
      把**上一个请求**的先验分解写进本次响应。
      实测这一轮的 `hypotheses` 是**空的**、终止原因是"没有任何证据可评（本次没有
      接入取证执行器）→ 拒答"，却照报上一次的 `by_category` —— **自相矛盾**
      （而且顺序调用即可复现，不需要并发）。

    ★ 为什么把初始值放在这里、而不是在某个分支里补一句：
      这是一条"**缺口 = 空**"的不变量 —— 将来任何新的早退路径都不会漏。
      兜底只留给"打分器**根本没提供** `snapshot()`"这一种情形（见 `facts`）。

    ★ 与 `flags` 的注释一致：它**不进检查点**。恢复时这里会被重新求值，
      所以"恢复后还没打分"同样等于空，不会带上一次运行的值。
    """
    return {"prior_snapshot": dict(EMPTY_PRIOR_SNAPSHOT)}


@dataclass(frozen=True)
class TaskBinding:
    task_id: str
    action: str
    outcome: Any = None

    @classmethod
    def from_mapping(cls, value):
        return cls(task_id=value.get('task_id',''),action=value.get('action',''),outcome=value.get('outcome'))


@dataclass
class GraphState:
    """状态图的工作状态（进程内；`to_agent_state()` 才是对外契约断面）。"""

    incident_id: str
    session_id: str
    trace_id: str
    user_message: str
    slots: Slots
    budget: Budget = field(default_factory=Budget)

    # ---- 意图层 ----
    intent: Optional[Intent] = None
    route: Optional[RouteOutcome] = None        # 路由留痕（解释性，不进契约）
    reference: Optional[ReferenceResolution] = None

    # ---- 取证层 ----
    round: int = 1
    plan: Optional[PlanOutcome] = None
    outcomes: list[AgentOutcome] = field(default_factory=list)
    board: Optional[EvidenceBoard] = None
    degraded_signals: list[str] = field(default_factory=list)   # ★ 源级闭集
    degraded_detail: list[str] = field(default_factory=list)    # 细信号 + 原因
    empty_signals: list[str] = field(default_factory=list)      # "查了没异常"

    # ---- 归因层（RCA → Critic → 仲裁）----
    hypotheses: list[Hypothesis] = field(default_factory=list)
    #: 仲裁结论（`ArbitResult`）—— **质量判据的权威来源**（`should_terminate` 读它）
    arbitration: Optional[ArbitResult] = None
    #: Critic 的留痕（排除了谁、为什么；`CriticReport.to_dict()`）
    critic: Optional[dict[str, Any]] = None
    open_questions: list[str] = field(default_factory=list)
    directed_rounds: int = 0

    # ---- 终止与产出 ----
    termination: Optional[TerminationDecision] = None
    status: str = "running"
    report: str = ""
    notes: list[str] = field(default_factory=list)
    #: 每轮的"新增有效证据"增益（≥2 条才可能触发"连续两轮无增益"）
    evidence_gains: list[float] = field(default_factory=list)
    #: 图内传递的**进程内**标志与句柄（黑板存储、用量账本、澄清裁决……）。
    #: ★ 刻意**不进检查点**：里面有活对象（存储句柄）与不可 JSON 化的裁决，
    #:   硬序列化只会造出"看起来恢复了、其实是假的"状态。
    flags: dict[str, Any] = field(default_factory=_initial_flags, repr=False)
    task_binding: TaskBinding | None = field(default=None, repr=False)  # Fresh runtime port; never checkpointed.
    started_at: float = field(default_factory=time.monotonic)

    # ------------------------------------------------------------------
    @property
    def evidence(self) -> list[Evidence]:
        return [item for outcome in self.outcomes for item in outcome.evidence]

    @property
    def elapsed_sec(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    def add_note(self, text: str) -> None:
        if text and text not in self.notes:
            self.notes.append(text)

    # ------------------------------------------------------------------
    def to_agent_state(self) -> AgentState:
        """转成对外的 `AgentState`（**真的过校验**）。

        ★ 这一句是 `degraded_signals` 闭集校验真正生效的地方：
          源级信号名写错会在这里抛，而不是把坏数据发给下游。
        ★ 黑板缺失 / trace_id 为空时**抛错**，不拿示例对象顶上 ——
          "契约字段缺失"要响，"发明一个看起来合理的值"会让下游以为这次排查真有黑板。
        """
        if self.board is None:
            raise ValueError(
                "GraphState.board 为空 —— AgentState 要求黑板必须存在（spec §7.1）；"
                "编排层应当在 incident 建立时就创建黑板（哪怕是空板）")
        if not self.trace_id:
            raise ValueError("GraphState.trace_id 为空 —— AgentState 要求 trace_id 非空")
        return AgentState(
            board=self.board, slots=self.slots,
            intent=self.intent.value if self.intent is not None else None,
            round=self.round,
            budget=self.budget.limits(),
            cost=self.budget.usage(),
            proposed_action=None, approval=None,
            status=self.status, trace_id=self.trace_id,
            degraded_signals=sorted(set(self.degraded_signals)),
        )

    # ------------------------------------------------------------------
    def to_checkpoint(self) -> dict[str, Any]:
        """可落盘的状态（**契约对象整份保留**，解释性留痕退化成摘要）。"""
        return {
            "incident_id": self.incident_id,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "user_message": self.user_message,
            "slots": self.slots.model_dump(mode="json"),
            "intent": self.intent.value if self.intent is not None else None,
            "round": self.round,
            "directed_rounds": self.directed_rounds,
            "budget": {**self.budget.limits(), **self.budget.usage(),
                       "aborted": self.budget.aborted, "notes": list(self.budget.notes)},
            "evidence": [item.model_dump(mode="json") for item in self.evidence],
            "hypotheses": [item.model_dump(mode="json") for item in self.hypotheses],
            "arbitration": (self.arbitration.model_dump(mode="json")
                            if self.arbitration is not None else None),
            "critic": dict(self.critic) if self.critic else None,
            "open_questions": list(self.open_questions),
            "degraded_signals": list(self.degraded_signals),
            "degraded_detail": list(self.degraded_detail),
            "empty_signals": list(self.empty_signals),
            "evidence_gains": list(self.evidence_gains),
            "status": self.status,
            "report": self.report,
            "notes": list(self.notes),
            "saved_at": datetime.now().astimezone().isoformat(),
            "attribution_v2": self.flags.get('attribution_v2'),
        }

    @classmethod
    def from_checkpoint(cls, payload: dict[str, Any], *,
                        budget: Optional[Budget] = None) -> "GraphState":
        """从检查点恢复。

        ★ 恢复的**只有契约对象与计数**：`route` / `plan` / `outcomes` 是解释性留痕，
          恢复成"未运行"是诚实的（它们的原始形态没法进 JSON，硬塞一份伪造的
          摘要比没有更糟）。`notes` 里会写明"这是从检查点恢复的状态"。
        """
        saved = dict(payload.get("budget") or {})
        restored = Budget(
            max_rounds=int(saved.get("max_rounds", Budget.max_rounds)),
            max_llm_calls=int(saved.get("max_llm_calls", Budget.max_llm_calls)),
            max_tool_calls=int(saved.get("max_tool_calls", Budget.max_tool_calls)),
            max_tokens=int(saved.get("max_tokens", Budget.max_tokens)),
            max_wall_time_sec=int(saved.get("max_wall_time_sec", Budget.max_wall_time_sec)),
            rounds=int(saved.get("rounds", 0)), llm_calls=int(saved.get("llm_calls", 0)),
            tool_calls=int(saved.get("tool_calls", 0)), tokens=int(saved.get("tokens", 0)),
            reserved_tokens=int(saved.get("reserved_tokens", 0)),
            elapsed_sec=float(saved.get("wall_time_sec", 0.0)),
            aborted=bool(saved.get("aborted", False)),
            notes=list(saved.get("notes") or []),
        )
        if budget is not None:            # 调用方给了新预算 → 上限以新的为准，消耗沿用
            restored.max_rounds = budget.max_rounds
            restored.max_llm_calls = budget.max_llm_calls
            restored.max_tool_calls = budget.max_tool_calls
            restored.max_tokens = budget.max_tokens
            restored.max_wall_time_sec = budget.max_wall_time_sec

        evidence = [Evidence.model_validate(item) for item in payload.get("evidence") or []]
        state = cls(
            incident_id=str(payload.get("incident_id") or ""),
            session_id=str(payload.get("session_id") or ""),
            trace_id=str(payload.get("trace_id") or ""),
            user_message=str(payload.get("user_message") or ""),
            slots=Slots.model_validate(payload.get("slots") or {}),
            budget=restored,
            intent=(Intent(payload["intent"]) if payload.get("intent") else None),
            round=int(payload.get("round") or 1),
            directed_rounds=int(payload.get("directed_rounds") or 0),
            hypotheses=[Hypothesis.model_validate(item)
                        for item in payload.get("hypotheses") or []],
            arbitration=(ArbitResult.model_validate(payload["arbitration"])
                         if payload.get("arbitration") else None),
            critic=(dict(payload["critic"]) if payload.get("critic") else None),
            open_questions=list(payload.get("open_questions") or []),
            degraded_signals=list(payload.get("degraded_signals") or []),
            degraded_detail=list(payload.get("degraded_detail") or []),
            empty_signals=list(payload.get("empty_signals") or []),
            evidence_gains=[float(x) for x in payload.get("evidence_gains") or []],
            status=str(payload.get("status") or "running"),
            report=str(payload.get("report") or ""),
            notes=list(payload.get("notes") or []),
        )
        if evidence:
            # 证据按"角色"归位成一个 outcome —— 角色署名在证据里（隔离的既成事实）
            by_role: dict[str, list[Evidence]] = {}
            for item in evidence:
                by_role.setdefault(item.agent_role, []).append(item)
            state.outcomes = [AgentOutcome(role=role, evidence=items)
                              for role, items in by_role.items()]
        state.add_note(f"从检查点恢复（{payload.get('saved_at') or '未知时间'}）；"
                       f"取证轮次={state.round}、证据={len(evidence)} 条")
        if payload.get('attribution_v2') is not None:
            from ..attribution.v2.contracts import Snapshot
            state.flags['attribution_v2']=Snapshot.model_validate(payload['attribution_v2']).model_dump(mode='json')
        return state

    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "session_id": self.session_id,
            "round": self.round,
            "intent": self.intent.value if self.intent is not None else None,
            "status": self.status,
            "budget": self.budget.to_dict(),
            "evidence": len(self.evidence),
            "hypotheses": [{"id": h.hypothesis_id, "score": round(h.score, 4),
                            "status": h.status.value} for h in self.hypotheses],
            "degraded_signals": list(self.degraded_signals),
            "empty_signals": list(self.empty_signals),
            "open_questions": list(self.open_questions),
            "evidence_gains": list(self.evidence_gains),
            "termination": self.termination.to_dict() if self.termination else None,
            "notes": list(self.notes),
        }

    def __str__(self) -> str:                       # pragma: no cover - 排障用
        return json.dumps(self.to_dict(), ensure_ascii=False)
