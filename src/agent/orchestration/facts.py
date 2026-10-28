"""给服务壳渲染的**结构化事实**（文案不在这里写）。

★ 本模块的方法原样搬自 `orchestrator.py` 的 `Orchestrator`，
  作为 mixin 参与 `Orchestrator` 的组装（**没有第二份实现**）。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from ..attribution import RcaAgent
from ..attribution.errors import audit_errors
from ..attribution.evidence_facts import GROUNDING_KEY
from .clarify import ClarifyDecision
from .hypotheses import SCORING_NOTE
from .state import EMPTY_PRIOR_SNAPSHOT, GraphState


class FactsMixin:
    """给服务壳渲染的**结构化事实**（文案不在这里写）。"""
    def _facts(self, state: GraphState) -> dict[str, Any]:
        """一次排查的**结构化事实**（服务壳据此渲染文案；这里不写文案）。

        ★ 本方法曾有 74 行。现在它只做**组装** ——
          三块内容各自一个方法（假设 / 预算 / 解释），字段名与取值一字未改。
        """
        clarify: Optional[ClarifyDecision] = state.flags.get("clarify")
        return {
            "status": state.status,
            "intent": state.intent.value if state.intent is not None else None,
            "rounds": state.round,
            "tool_calls": state.budget.tool_calls,
            "evidence_count": len(state.evidence),
            "query_coverage": state.flags.get('query_coverage'),
            "metric_localization": state.flags.get('metric_localization'),
            "degraded_signals": list(state.degraded_signals),
            "degraded_detail": list(state.degraded_detail),
            "empty_signals": list(state.empty_signals),
            "open_questions": list(state.open_questions),
            "hypotheses": _hypothesis_facts(state),
            # ★★ 跨服务的错误汇总（谁在报错 + 各自什么错 + 是不是只是替下游报错）。
            #   以前这些信息只在日志证据的**文本**里 —— facts 里没有，
            #   模型只能说"事实里没给错误类型"（明明采到了却说没有）。
            # ★ 候选（`intrinsic` / `propagated` / `slow`）与指纹一起进去：
            #   它们是**代码枚举**的，模型只能在候选内排序，不许自由发挥。
            "error_summary": audit_errors(
                state.evidence, slots=state.slots,
                candidates=_candidates(state))["error_summary"],
            # ★ 本轮的**结论**（服务 / 错误类型 / 位置 / 次数 / 首现 / 是否传播）：
            #   由代码的地板 + 模型在候选内的选择合成；模型越界时这里只剩地板。
            "root_cause_conclusions": _conclusion_facts(state),
            "clarify": clarify.to_dict() if clarify is not None else None,
            "termination": state.termination.to_dict() if state.termination else None,
            # ★ 根因（实体与叙事主语**同源**：都取候选内裁决选出的主语）
            "root_cause": _root_cause_dict(state.arbitration, _grounding(state)),
            # ★★ 查数值的结果（契约修订 21）：值 + 算法 + 步骤 + 口径 ——
            #   它是"答案"的全部依据，渲染层据此写正文（不再依赖假设/仲裁）。
            "metric_answer": (state.flags or {}).get("metric_answer") or None,
            "qa_result":state.flags.get('qa_result'),
            "arbitration": (state.arbitration.model_dump(mode="json")
                            if state.arbitration is not None else None),
            "critic": state.critic,
            # ★ 变更预案与风险分级（写意图才有；执行端在后续阶段）
            "proposed_action": (state.flags["action"].model_dump(mode="json")
                                if state.flags.get("action") is not None else None),
            "risk": (state.flags["risk"].model_dump(mode="json")
                     if state.flags.get("risk") is not None else None),
            "budget": self._budget_facts(state),
            # ★ 认出了动作但没认出目标 → 不提案，且要把原因说出来
            "action_blocked": state.flags.get("action_blocked") or None,
            "explanation": self._explanation_facts(state),
            "notes": list(state.notes),
        }

    def _budget_facts(self, state: GraphState) -> dict[str, Any]:
        """预算的上限 / 消耗 / **逐项是否真的生效** / 备注。

        ★★ `enforced` 每一项都必须有**真的读取点**（见 `_enforcement`），
          不许硬编码 `True`。
        """
        return {
            "limits": state.budget.limits(),
            "used": state.budget.usage(),
            "enforced": self._enforcement(state),
            "notes": list(state.budget.notes[-4:]),
        }

    def _explanation_facts(self, state: GraphState) -> dict[str, Any]:
        """解释性内容：打分口径 / 先验分解 / 指代消解 / 升级（慢调用）/ 图。

        ★★ 先验分解与"被下限截断的类别"要**显式暴露**（不静默）——
          `PRIOR_FLOOR` 一旦把某类别的先验抬起来，那个先验就不再完全是数据算出来的。
        """
        trace = getattr(state.route, "trace", None)
        model_calls = [call.to_dict() for call in getattr(trace, "model_calls", ()) or ()]
        slow = [call for call in model_calls if call.get("stage") == "slow"]
        return {
            "scoring": ('必要条件满足比例；确认另需独立可回查支持及反证检查，非概率'
                        if state.flags.get('attribution_v2') is not None else SCORING_NOTE),
            "scoring_kind": ('obligations-v1' if state.flags.get('attribution_v2') is not None
                             else _scoring_kind(self.scorer)),
            "attribution_v2": state.flags.get('attribution_v2'),
            "query_coverage": state.flags.get('query_coverage'),
            "metric_localization": state.flags.get('metric_localization'),
            "attribution_path": state.flags.get('attribution_path','v2'),
            "prior": self._prior_explanation(state),
            "reference": state.reference.to_dict() if state.reference else None,
            "escalation": {
                "escalated": bool(slow),
                "slow_latency_ms": round(
                    max((call.get("latency_ms", 0.0) for call in slow), default=0.0), 1),
                "model_calls": model_calls,
            },
            "graph": {"nodes": list(self.graph.nodes),
                      "entry": self.graph.entry, "terminal": self.graph.terminal,
                      "conditional": self.graph.conditional_node},
        }

    # ------------------------------------------------------------------
    #  给服务壳/响应用的结构化事实（**文案由服务壳渲染**）
    # ------------------------------------------------------------------
    def _enforcement(self, state: GraphState) -> dict[str, bool]:
        """`budget.enforced` 的**真值**（逐项回答"这一项本次到底拦不拦"）。

        | 字段 | 真值从哪来 |
        |---|---|
        | `max_rounds` | 轮次闸门**每次 run 都在**（`Budget.has_round_left()` 真会拒），所以只要上限是个正数就为 True |
        | `token_budget` | 同上：Token 闸门在 `Budget.exhausted()` 里真会拒；上限为 0 时它没有意义，如实报 False |
        | `dry_run_only` | **客户端要求了**就为 True —— 因为要求了就一定**真的拦**（`_prepare_action`）。没有"要求了但没拦"的情形：拦住的是"产出可落地预案"这个动作本身，而它在 `_prepare_action` 里是必经之路 |
        | `allow_write` | 本 run **有没有**一个能执行写入的执行端（`interfaces.ActionExecutor`） |

        ★ 这里**不许写死 `True`**：写死的那一刻，`enforced` 就不再是"有没有拦住"，
          而只是一句口号 —— 用户看到 `enforced=true` 却拦住不拦无从核对。

        ★ `dry_run_only` 语义（写死在这里，避免两处口径）：
          客户端置 `true` = "这次只许干跑"。那么即使识别出 L2 重启，也**不产出**
          可落地的 `ProposedAction`，只留风险分级 + 拦截原因（用户照样看得到"这本该是 L2"）。
          没要求（`false`）→ 该字段为 `False`：**不是"没强制"，而是"这一条没被要求"**。
        """
        requested = bool(state.flags.get("dry_run_only"))
        limits = state.budget.limits()
        return {
            # ★ 真值来自"闸门存在且上限是个有意义的正数"（0 = 没有闸门可言）
            "max_rounds": int(limits.get("max_rounds") or 0) > 0,
            "token_budget": int(limits.get("max_tokens") or 0) > 0,
            # ★ 只有"客户端要求了"才为真 —— 不是无条件 `True`。
            #   要求了就必须真的拦住可落地预案，见 `_prepare_action`。
            "dry_run_only": requested,
            # ★ 真值来自"本 run 有没有执行端"，而不是写死 False
            "allow_write": getattr(self, "action_executor", None) is not None,
        }

    def _prior_explanation(self, state: GraphState) -> dict[str, Any]:
        """先验的可解释出口：分解 + **被下限截断的类别**。

        ★ 判据：`PRIOR_FLOOR` 抬起来的先验不是"算出来的"，所以不能只留在内部变量里。
          响应里必须能看到"哪几个类别的先验被抬过" —— 否则审计时无法区分
          "这条候选靠证据排第一"与"靠地板排第一"。

        ★★ **只读随本次请求带下来的快照**（打分那一刻取的，
          见 `hypotheses_node._score_candidates`）。以前读的是打分器实例上的字段，
          而那是个**进程级单例** —— 从打分到这里隔着多次 await，并发请求会把
          对方的值写进来：实测 A 的 `hypotheses` 是 A 自己的，`prior.by_category`
          却是 B 的（"机器可核对的误导性声明"）。
          实例字段只在"打分器没提供 `snapshot()`"时作兜底。
        """
        snapshot = state.flags.get("prior_snapshot")
        if not isinstance(snapshot, Mapping):
            # `flags` 的初始值里**本来就有**一个空快照（`state._initial_flags`），
            # 所以走到这里只有两种可能：
            #  · 打分器**没实现** `snapshot()`（替换实现）→ 只能回读实例字段（并发下
            #    可能读到别的请求的值，这是替换实现自身的限制，如实保留）；
            #  · 其它任何情况（状态被非常规构造、字段被替换掉）→ 按**空**处理：
            #    "没打分" = 没有任何先验产物可报，**绝不**回读上一次的。
            if callable(getattr(self.scorer, "snapshot", None)):
                snapshot = dict(EMPTY_PRIOR_SNAPSHOT)
            else:
                snapshot = {
                    "by_category": dict(getattr(self.scorer, "last_priors", {}) or {}),
                    "floored_categories": sorted(
                        getattr(self.scorer, "floored_categories", ()) or ()),
                }
        floored = sorted(snapshot.get("floored_categories") or ())
        by_category = dict(snapshot.get("by_category") or {})
        return {
            "formula": "Prior(H) = Σ_c w_c · strength_c · affinity[H][c]（加权平均，带下限）",
            "by_category": by_category,
            "floored_categories": floored,
            "floor_note": (f"这些类别的先验被下限抬到 PRIOR_FLOOR：{'、'.join(floored)}"
                           f" —— 它们的先验**不完全是数据算出来的**"
                           if floored else "没有类别的先验被下限截断"),
        }

def _grounding(state: GraphState) -> dict[str, Any]:
    """本轮取证阶段算出来的裁决上下文（候选 / trace JSON / 地板 / 结论）。"""
    found = (getattr(state, "flags", None) or {}).get(GROUNDING_KEY)
    return dict(found) if isinstance(found, Mapping) else {}


def _candidates(state: GraphState) -> list[dict[str, Any]]:
    """代码枚举出来的错误候选（没跑到取证那一步就是空列表 —— **不编**候选）。"""
    return [dict(item) for item in (_grounding(state).get("candidates") or [])]


def _conclusion_facts(state: GraphState) -> list[dict[str, Any]]:
    """本轮的结论条（模型在候选内的选择；越界时是代码的地板）。"""
    grounding = _grounding(state)
    decision = grounding.get("decision")
    if not isinstance(decision, Mapping):
        return []
    return [dict(item) for item in (decision.get("verdicts") or [])]


def _hypothesis_facts(state: GraphState) -> list[dict[str, Any]]:
    """★ 形状对齐响应契约（`rank` / `statement` / `score` /
    `status` / `supporting` / `excluded_reason`）—— 外部验收 A10 会逐项断言。"""
    return [
        {"rank": index,
         "hypothesis_id": item.hypothesis_id,
         "statement": item.statement,
         "category": item.category,
         "score": round(item.score, 4),
         "prior": round(item.prior, 4),
         "status": item.status.value,
         "supporting": list(item.supporting),
         "excluded_reason": item.excluded_reason}
        for index, item in enumerate(
            sorted(state.hypotheses, key=lambda h: -h.score), start=1)]


def _scoring_kind(scorer: Any) -> str:
    """打分口径的**如实标注**：用的是真公式还是临时基线（消融对照时会是后者）。"""
    if isinstance(scorer, RcaAgent):
        return "weighted-prior+softmax(D1/D2)"
    return f"provisional({type(scorer).__name__})"

def _root_cause_dict(arbitration: Any,
                     grounding: Optional[Mapping[str, Any]] = None
                     ) -> Optional[dict[str, Any]]:
    """把 `ArbitResult.root_cause` 渲染成 **响应形状**。

    spec 的例子是 `{"statement", "entity", "category", "score"}`，其中
    `entity` 是**实体 id 串**（如 `svc:order@prod-cn-1`）—— 不是对象。
    另外附上 `hypothesis_id` 与 `supporting`（证据链），客户端可直接展示。

    ★★ `service` / `propagated` / `downstream` 三项来自**候选内裁决的主语**
      （`grounding["subject"]`）：前四项是打分框架的产物，这三项回答
      "这个根因落在哪个服务、是不是只是**传播/影响面**" ——
      没有它，客户端只能看到 `svc:order@…`，读不出"order 只是替下游喊了一声"。
    """
    if arbitration is None or getattr(arbitration, "root_cause", None) is None:
        return None
    root = arbitration.root_cause
    subject = (grounding or {}).get("subject")
    subject = dict(subject) if isinstance(subject, Mapping) else {}
    return {
        "statement": root.statement,
        "entity": root.root_entity.id,
        "category": root.category,
        "score": round(root.score, 4),
        "confidence": (round(arbitration.confidence, 4)
                       if getattr(arbitration, "confidence", None) is not None else None),
        "hypothesis_id": root.hypothesis_id,
        "supporting": list(root.supporting),
        "contradicting": list(root.contradicting),
        # ★ 服务维度：与叙事主语同源（候选内裁决选出的那一条）
        "service": root.root_entity.name,
        "propagated": bool(subject.get("propagated")),
        "downstream": str(subject.get("downstream") or ""),
    }
