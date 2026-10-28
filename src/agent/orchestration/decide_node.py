"""⑥ decide 节点：终止判据与状态收口。

★ 本模块的方法原样搬自 `orchestrator.py` 的 `Orchestrator`，
  作为 mixin 参与 `Orchestrator` 的组装（**没有第二份实现**）。
"""

from __future__ import annotations

from ..common.enums import ArbitDecision, Intent
from ..common.steps import emit_step
from typing import Any

from .state import GraphState, TOP2_CLOSE, TerminationDecision, should_terminate
from .constants import CAN_PRODUCE_ROOT_CAUSE, EVIDENCE_INTENTS, logger


class DecideMixin:
    """⑥ decide 节点：终止判据与状态收口。"""
    # ------------------------------------------------------------------
    #  ⑥ decide：终止判据（**唯一**调用点）
    # ------------------------------------------------------------------
    async def _node_decide(self, state: GraphState) -> str:
        if state.flags.get('attribution_v2') is not None:
            data=state.flags['attribution_v2']
            status='concluded' if self._has_root_cause(state) else 'abstained'
            if '预算耗尽' in data['stop_reason']:status='budget_exhausted'
            state.termination=TerminationDecision(terminate=True,kind='attribution_v2',
                status=status,reason=data['stop_reason'])
            self._apply_status(state,state.termination)
            emit_step(lambda event,data:self._emit(state,event,data),'decide','按问题裁决与收口',
                {'problems':len(data['problems']),'round':state.round},result=data['stop_reason'])
            return 'report'
        can_direct, why = self._directed_target(state)
        # ★ "预算耗尽"要分两种（否则状态会误导人）：
        #   · 还有想查的东西、只是没额度了 → `budget_exhausted`（**被预算剥夺**）；
        #   · 本来就没有可定向的东西 → 那不是"预算不够"，而是"查清楚了/查不动了"
        #     → 按"无新增证据"收口（`concluded`/`abstained`），理由里照样写明轮次用满。
        #   实测踩到过：客户端把 `max_rounds` 设成 1 时，每一轮成功完成都会报
        #   `budget_exhausted` —— 语义上没错，但读起来像失败。
        has_target, _ = self._directed_target(state, ignore_budget=True)
        decision = should_terminate(state)
        if decision.terminate and decision.kind == "budget" and not has_target:
            decision = self._terminal_without_target(state, decision.reason)
        elif not decision.terminate and not can_direct:
            decision = self._terminal_without_target(state, why)
        elif decision.terminate and self._no_evidence_at_all(state):
            # ★ "收敛输出最佳"但**根本没有证据** → 不能假称有结论（只能拒答）。
            #   注意：**保留原来的 kind**（例如 `no_progress`）—— "为什么停"与
            #   "停了之后能不能下结论"是两个问题，前者不该被后者抹掉。
            decision = self._terminal_without_target(state, decision.reason,
                                                     kind=decision.kind)
        state.termination = decision
        self._apply_status(state, decision)
        state.add_note(decision.reason)
        logger.info("终止判据：%s（%s）", decision.kind, decision.reason)
        # ★★ 裁决也是一次"工具调用"（契约修订 20）：用户要看得到"为什么停/为什么没结论"，
        #   而不是只在折叠区里看到一句内部叙述。
        root = (state.arbitration.root_cause if state.arbitration is not None else None)
        emit_step(lambda event, data: self._emit(state, event, data), "decide", "裁决与收口",
                  {"kind": decision.kind, "round": state.round,
                   "evidence": len(state.evidence),
                   "hypotheses": len(state.hypotheses or ())},
                  result=f"状态 {state.status}；判据 {decision.kind}；"
                         f"{'已定位根因：' + str(getattr(root, 'statement', ''))[:120] if root else '没有根因（如实拒答/收口）'}")
        return "report" if decision.terminate else "directed"

    def _apply_status(self, state: GraphState, decision: Any) -> None:
        """把终止判据落成 `status` —— **两个节点共用这一处**（口径不会分叉）。

        ★★ `concluded` 的语义是"**已定位根因**"（客户端据此展示根因 + 证据链）。
          所以这条规则不是"临时保守"，而是**永久契约**：

              `concluded` ⟺ 手里**真的**有根因（`ArbitResult.root_cause` 非空）

          没有根因的收口一律落 `abstained`（spec 原文：拒答是**正确行为**，
          客户端展示"已收集证据 + 建议人工方向"）。三种常见情形：
            · 该意图本阶段没有落地查询路径（`METRIC_QUERY` 等）；
            · 证据不足/候选接近/全部低分（仲裁拒答）；
            · 预算耗尽（那是 `budget_exhausted`，优先级更高）。

        ★ 这一条必须在**所有**设置状态的地方生效 —— 只在 `decide` 里做会漏掉
          "该意图不取证"那条路（实测：澄清预算用例里仍然是 concluded）。
        """
        state.status = decision.status if decision.terminate else "running"
        self._guard_concluded(state)
        self._guard_answered(state)

    @staticmethod
    def _guard_answered(state: GraphState) -> bool:
        """查数值这一类：**只要真算出了数**，状态就是 `answered`（不是"未得出结论"）。

        ★★ 契约修订 21 的语义边界：`concluded` = 已定位根因；`answered` = 已给出所问的结果。
          "QPS 是多少"给出一个数就是**答了**（用户实测原话："都是 0，就该总结出 QPS=0"），
          记成 `abstained`（"未得出结论"）是假话 —— 系统明明算出来了。
        ★ 只对**值问答**生效（`METRIC_QUERY` 且这一轮真的走了分析），且**不改**其它状态
          （预算耗尽 / 拒答 / 失败一律保留）。
        """
        from ..common.enums import ChatStatus  # noqa: PLC0415 —— 避免模块级循环引用

        if state.status != ChatStatus.ABSTAINED.value or state.hypotheses:
            return False
        result = (state.flags or {}).get("metric_answer") or {}
        value = (result.get("answer") or {}).get("value")
        if value is None:
            return False
        state.status = ChatStatus.ANSWERED.value
        state.add_note("查数值：已算出结果 → 状态记为 answered（已给出结果，不是故障结论）")
        return True

    def _guard_concluded(self, state: GraphState) -> bool:
        """把 `status` 掉出 `concluded` 的**唯一判据**（所有写状态处共用）。

        ★★ `concluded` 的语义是"**已定位根因**"。所以这条判据必须在**每一处**把
          状态定成 `concluded` 之后跑一遍 —— 包括 `report` 节点那条"兜底收口"。
          各写一份的后果实测过：`report` 里写死 `"concluded"` 会把没有根因的收口
          报成"已定位根因"（红线：结论必须有证据）。

        Returns:
            是否**发生了降级**（`concluded` → `abstained`）。
        """
        if state.status != "concluded" or self._has_root_cause(state):
            return False
        state.status = "abstained"
        state.add_note(
            "★ 这次收口**没有根因**（`concluded` 的语义是『已定位根因』）"
            "→ 状态如实记为 `abstained`：展示已收集的证据、候选假设与建议方向，"
            "不给没有依据的根因")
        return True

    @staticmethod
    def _has_root_cause(state: GraphState) -> bool:
        """这次排查手里有没有根因（`concluded` 的唯一前提）。"""
        return bool(CAN_PRODUCE_ROOT_CAUSE and state.arbitration is not None
                    and state.arbitration.root_cause is not None)

    def _no_evidence_at_all(self, state: GraphState) -> bool:
        """需要取证却一条证据都没有（执行器没接 / 全路都没查成）。

        ★ 判据用 `EVIDENCE_INTENTS`（完整链路 ∪ 轻链路）——
          轻链路也是"要取证"的意图，一条证据都没有同样不能假称有结论。
        """
        return (state.intent in EVIDENCE_INTENTS and not state.evidence)

    def _terminal_without_target(self, state: GraphState, why: str, *,
                                 kind: str = "no_progress"):
        """没有可定向的证据 → 收口（**不是**"再查一轮看看"）。

        ★ 这一条不在那五条终止条件里，但它是"图必须有出口"的必要条件：
          否则图会在 `decide → directed → forensics` 上转到预算耗尽 —— 那是**假收敛**
          （用户等了 300s，拿到的还是同样不确定的结论）。所以把它写成显式的"无增益"。

        ★ 收口成 `concluded` 还是 `abstained`，由**意图与证据**决定：
          · 越界/知识型（政策口径）→ `abstained`（既有语义，不变）；
          · 需要取证却一条证据都没有 → `abstained`（**没有证据可评就不给结论**）；
          · 其余（查询/变更影响这类本阶段无落地路径的意图）→ `concluded` + 建议方向。
        """
        if state.intent in (Intent.OUT_OF_SCOPE, Intent.KNOWLEDGE_QA):
            return TerminationDecision(
                terminate=True, kind="abstain", status="abstained",
                reason=(f"{state.intent.value} 按 spec §3.1 走拒答/降级（决策记录 C4）"),
            )
        if self._no_evidence_at_all(state):
            return TerminationDecision(
                terminate=True, kind=kind, status="abstained",
                reason=(f"没有任何证据可评（{why}）→ **拒答**："
                        f"没有证据就下根因结论等于编（spec §2 H2/H3）"),
            )
        return TerminationDecision(
            terminate=True, kind=kind, status="concluded",
            reason=(f"没有可定向的区分性证据（{why}）→ 收口并输出当前最佳候选；"
                    f"再查一轮不会更清楚（spec §3.5 的『无新增有效证据』口径）"),
        )

    def _directed_target(self, state: GraphState, *,
                         ignore_budget: bool = False) -> tuple[bool, str]:
        """要不要（能不能）再定向查一轮？返回 `(能, 原因)`。

        `ignore_budget=True` 只回答"**有没有**值得再查的东西"，不看还剩多少额度 ——
        `decide` 用它区分"被预算剥夺"与"本来就没得查"（两种状态含义不同）。
        """
        if state.flags.get("forensics_not_applicable"):
            return False, (f"{state.intent.value if state.intent else '该意图'} 在 P1 "
                           f"不取证（T21 接查询路径）")
        if state.flags.get("forensics_skipped"):
            return False, "本次没有接入取证执行器"
        if not ignore_budget and not state.budget.has_round_left():
            return False, (f"轮次预算已用满（{state.budget.rounds}/"
                           f"{state.budget.max_rounds}）")
        reasons: list[str] = []
        # ★ **仲裁明确要"定向取证"**时优先听它的（它看得见完整分数分布）
        if state.arbitration is not None and \
                state.arbitration.decision is ArbitDecision.NEED_TARGETED_EVIDENCE:
            reasons.append(f"仲裁要求定向取证（{state.arbitration.report or ''}）")
        top = state.hypotheses[:2]
        if len(top) == 2 and abs(top[0].score - top[1].score) < TOP2_CLOSE:
            reasons.append(f"top-2 假设接近（{top[0].score:.2f} vs {top[1].score:.2f}）")
        if state.degraded_signals:
            reasons.append(f"有信号源没查成（{'、'.join(state.degraded_signals)}）")
        if not reasons:
            return False, "既没有接近的候选、也没有缺失的信号源"
        if ignore_budget and not state.budget.has_round_left():
            return True, ("；".join(reasons) + f"（但轮次预算已用满 "
                                               f"{state.budget.rounds}/"
                                               f"{state.budget.max_rounds}）")
        return True, "；".join(reasons)
