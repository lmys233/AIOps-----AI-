"""确认之后怎么走：**言行分开**（T59c）。

★★ 为什么单列一层：「确认」不是普通全局意图，而是**带目标的对话行为** ——
执行依据必须是**结构化待确认状态**（`PendingConfirmation`），模型只能辅助"是不是在确认"，
不能决定"确认的是哪一件事"。识别与取舍在 `speech_act`（识别 / 匹配 / 决策表）；本层只把
决策**落到流程**上，逐类型分工：

| 分支 | 类型 | 结果 |
|---|---|---|
| 命题确认 | `proposition_confirmation` | **只更新结论状态**：把该 incident 记为"已被用户确认"，**不碰任何工具** |
| 行动确认 | `action_confirmation` | 走**既有确认结局**（禁止类拒绝 / 待人工审批 / 需执行端 / 写能力未开 → 拒绝）—— 四条结局都**不执行变更** |
| 选项选择 | `option_selection` | 交回台账：只决定"接着问哪一项"，**要用户给值**（不猜值） |
| 否定 / 取消 | `on_deny` | 把那条 pending 落成 `cancelled`，如实说"什么都没做" |

★★ **越界覆盖**：NLU 判越界但会话里还有待确认时，不礼貌拒答，而是**把话头接回 pending**
（念出候选 + 说明回「确认」即可）—— 依据仍是结构化状态，不是猜。
★ 本层**不执行任何变更**：写能力与审批红线全在既有链路里，一步不动。
★ 每一步都发成对的 `tool` 事件（`pending_match`）：行为 / 候选集 / 命中依据 / 分支结论，用户看得见。
"""

from __future__ import annotations
from ..observed.context import session_key


from typing import Any, Callable, Mapping, Optional, Sequence

from ..common.enums import ChatStatus, Intent
from ..common.steps import emit_step_phase
from .contract_body import empty_contract_body
from .idempotency import ensure_turn_id
from .pending import PendingConfirmation, PendingKind, PendingRegistry
from .speech_act import (CONFIRM_ACTS, Decision, SpeechAct, classify, decide,
                         describe_candidates, pick_option, refers_to_pending)
from .tasks import clear_parked, degraded_sink
from .util import _action_fingerprint

__all__ = ["PendingFlowMixin", "pending_decision_of"]


def pending_decision_of(registry: PendingRegistry, session_id: str, message: str, *,
                        now: Optional[float] = None) -> Decision:
    """把「这句话 + 会话里的待确认状态」判成一条决策（**确定性**，模型不参与）。

    ★ 不是确认类信号就**直接收手**（`none`）—— 普通问答、查指标、排查故障一律走原路，
      本层只在"用户在做确认类动作"时插手。
    ★ 过期的那几条**一并带上**：决策表要用它说"你那条已经过期了"，而不是装作没有。
    """
    act = classify(message)
    if act not in CONFIRM_ACTS:
        return Decision(action="none", reason=f"{act} 不是确认类信号")
    alive = registry.candidates(session_id, now=now)
    expired = [item for item in registry.candidates(session_id, now=now, include_expired=True)
               if item.status == "expired"]
    return decide(alive, act, message, expired=expired)


def _pending_params(decision: Decision, act: str,
                    candidates: Sequence[PendingConfirmation]) -> dict[str, Any]:
    """`tool` 事件的参数（有界、脱敏：行为 / 候选集 / 决策 / 依据）。

    ★ 候选集**列出来**（最多 4 条 id）—— "我为什么挑了这条"必须能从事件里复核，
      而不是只有一句"已按待确认状态处理"。
    """
    return {"act": str(act), "candidates": len(candidates),
            "candidate_ids": [item.pending_id for item in candidates[:4]],
            "decision": str(decision.action),
            "confidence": round(float(decision.confidence), 2),
            "basis": str(decision.reason)[:80]}


class PendingFlowMixin:
    """确认分派与分支处理（语义见模块文档）。"""

    async def maybe_pending_or_confirmation(self, request: Any, session: Any, *,
                                            trace_id: str,
                                            on_event: Any = None) -> Optional[dict[str, Any]]:
        """先按**结构化待确认状态**分派；没有结构化依据时再退回既有的「确认」链路。

        ★ 顺序不能反：结构化状态是"我确实在等你确认哪一件事"的**事实**；
          旧链路只看"有没有留存的待确认动作"，说不出"是哪一件"。
        """
        body = await self.maybe_pending_turn(request, session, trace_id=trace_id,
                                             on_event=on_event)
        if body is not None:
            return body
        return await self.maybe_plain_confirmation(request, session, trace_id=trace_id)

    async def maybe_pending_turn(self, request: Any, session: Any, *, trace_id: str,
                                 on_event: Any = None) -> Optional[dict[str, Any]]:
        """有可匹配的待确认 + 这句是确认类信号 → 由本层接手（否则返回 `None` 让原路走）。"""
        decision = pending_decision_of(self.pendings, request.session_id, request.message)
        if not decision.handled:
            # ★★ 不是确认类信号 = 用户在说**别的事** → 旧待确认**挂起不删**
            #   （这就是"切话题后回来仍能确认"的落地；别拿旧待确认去吞新问题）
            self._suspend_for_new_topic(request)
            return None
        act = classify(request.message)
        alive = self.pendings.candidates(request.session_id)
        sink = self._pending_sink(on_event, request, decision.pending, trace_id=trace_id)
        emit_step_phase(sink, "start", "pending_match", "按会话里的待确认状态处理",
                        _pending_params(decision, act, alive))
        body = await self._pending_body(request, session, decision, trace_id=trace_id)
        if body is None:
            return None
        body.setdefault("degraded", []).append(
            f"这句按会话里的待确认状态接手（{act} → {decision.action}）：{decision.reason}")
        await self._store_incident(body)
        emit_step_phase(sink, "done", "pending_match", "按会话里的待确认状态处理",
                        _pending_params(decision, act, alive), ok=True,
                        result=(f"{decision.action}（{decision.reason}）"
                                f"→ status={body.get('status')}"))
        return body

    async def _pending_body(self, request: Any, session: Any, decision: Decision, *,
                            trace_id: str) -> Optional[dict[str, Any]]:
        """决策 → 响应体（分支表就是这里的顺序）。"""
        if decision.action in ("clarify", "prompt_expired"):
            return self._pending_ask_body(request, decision, trace_id=trace_id)
        pending = decision.pending
        if pending is None:
            return None
        if decision.action == "ask_second_confirmation" and not pending.target.get("second_asked"):
            # ★ 高风险**不凭一句"确认"就往下走**：先二次确认（记录已问过，下一次才进分支）
            self.pendings.note(request.session_id, pending.pending_id,
                               target={"second_asked": True})
            return self._pending_ask_body(request, decision, trace_id=trace_id)
        act = classify(request.message)
        if act in (SpeechAct.DENY, SpeechAct.CANCEL):
            return self._pending_cancel_body(request, pending, trace_id=trace_id)
        if pending.confirmation_type == PendingKind.ACTION:
            return await self._pending_action_body(request, session, pending, trace_id=trace_id)
        if pending.confirmation_type == PendingKind.PROPOSITION:
            return await self._pending_proposition_body(request, pending, trace_id=trace_id)
        return self._pending_option_body(request, pending, trace_id=trace_id)

    # ------------------------------------------------------------------
    async def _pending_action_body(self, request: Any, session: Any,
                                   pending: PendingConfirmation, *,
                                   trace_id: str) -> dict[str, Any]:
        """行动确认 → **既有确认结局**（四态不变，一步都不执行）。"""
        self.pendings.resolve(request.session_id, pending.pending_id, status="resolved")
        stored = await self._pending.get(session_key(request.session_id))
        plan = pending.target.get("plan")
        if stored is None and isinstance(plan, Mapping) and plan:
            # ★ 确认门只有**候选方案**时也要能确认：用登记时存下的方案载荷走既有结局
            return await self._pending_plan_body(request, session, pending, plan, trace_id=trace_id)
        if stored is None:
            return self._pending_ask_body(request, Decision(
                action="clarify", pending=pending, confidence=1.0,
                question=("我已经记下你的确认，但这一轮**没有形成唯一的处置动作**"
                          "（当时只有候选方案）→ 请指明要执行哪一条方案，我再往下走")),
                trace_id=trace_id)
        body = await self._finish_confirmation(request.session_id, trace_id=trace_id,
                                               answers={"confirm": True})
        await clear_parked(session, request.session_id, degraded=degraded_sink(body))
        return body

    async def _pending_plan_body(self, request: Any, session: Any,
                                 pending: PendingConfirmation, plan: Mapping[str, Any], *,
                                 trace_id: str) -> dict[str, Any]:
        """首选项方案被确认之后：**人工类**如实说"我不代执行"；**可执行类**交既有确认结局。"""
        title = str(plan.get("title") or pending.target.get("content") or "该方案")[:80]
        if str(plan.get("kind") or "") != "agent_action":
            body = self._pending_base(request, trace_id=trace_id)
            body.update({
                "status": ChatStatus.REJECTED.value,
                "reject_reason": f"方案「{title}」属**人工操作**类：Agent 不代执行",
                "report": (f"已记下你的确认：方案「{title}」需要**人工执行**（步骤见上一条方案表）。"
                           f"我这边没有写通道 —— **没有应用任何变更**。")})
            await clear_parked(session, request.session_id, degraded=degraded_sink(body))
            return body
        action = dict(plan.get("action") or {})
        risk = dict(plan.get("risk") or {"level": "L2", "requires_approval": False})
        await self._pending.put(session_key(request.session_id), {
            "action": action, "risk": risk,
            "incident_id": str(pending.target.get("incident_id") or ""),
            "action_hash": _action_fingerprint(action),
            "allow_write": bool(getattr(request.options, "allow_write", False))})
        body = await self._finish_confirmation(request.session_id, trace_id=trace_id,
                                               answers={"confirm": True})
        await clear_parked(session, request.session_id, degraded=degraded_sink(body))
        return body

    async def _pending_proposition_body(self, request: Any, pending: PendingConfirmation, *,
                                        trace_id: str) -> dict[str, Any]:
        """命题确认 → **只更新结论状态**（不碰工具、不执行动作）。"""
        self.pendings.resolve(request.session_id, pending.pending_id, status="resolved")
        incident_id = str(pending.target.get("incident_id") or "")
        body = self._pending_base(request, trace_id=trace_id, incident_id=incident_id,
                                  intent_name=Intent.INCIDENT_RCA.value)
        base = await self.get_incident(incident_id)
        if base:
            body.update(base)
        body.update({"session_id": request.session_id, "trace_id": trace_id,
                     "turn_id": ensure_turn_id(request)})
        content = str(pending.target.get("content") or "该结论")[:80]
        if body.get("root_cause"):
            body.update({
                "status": ChatStatus.CONCLUDED.value,
                "report": (f"已按你的确认**记录结论**：{content}。"
                           f"★ 这一步只更新结论状态 —— **没有调用任何工具、"
                           f"没有执行任何动作**。")})
        else:
            body.update({
                "status": ChatStatus.CLARIFY.value,
                "clarification": {
                    "questions": [{"slot": "confirm",
                                   "question": "你要确认的是哪一件事？"
                                               "（我这边没有可确认的结论）"}],
                    "reason": "命题确认缺少可确认的结论 → 不假装确认过什么"},
                "report": "我这边**没有可确认的结论** —— 请先让我查一次，或说明你要确认什么。"})
        return body

    def _pending_cancel_body(self, request: Any, pending: PendingConfirmation, *,
                             trace_id: str) -> dict[str, Any]:
        """否定 / 取消 → 作废那一条（如实说"什么都没做"）。"""
        self.pendings.resolve(request.session_id, pending.pending_id, status="cancelled")
        content = str(pending.target.get("content") or "那件事")[:60]
        body = self._pending_base(request, trace_id=trace_id)
        body.update({
            "status": ChatStatus.REJECTED.value,
            "reject_reason": f"用户取消了待确认事项（{content}）—— 未执行任何变更",
            "report": (f"好，已把你那条待确认（{content}）**作废**："
                       f"没有任何变更被应用，也不会再拿它来问你。")})
        return body

    def _pending_option_body(self, request: Any, pending: PendingConfirmation, *,
                             trace_id: str) -> dict[str, Any]:
        """选项选择 → 只决定"接着问哪一项"（要值，不猜值、不执行）。"""
        chosen = pick_option(pending, request.message)
        label = str((chosen or {}).get("label") or "你选的那一项")[:80]
        body = self._pending_base(request, trace_id=trace_id)
        body.update({
            "status": ChatStatus.CLARIFY.value,
            "clarification": {
                "questions": [{"slot": str((chosen or {}).get("id") or "confirm"),
                               "question": f"好，那就先补这一项：{label}",
                               "pending_id": pending.pending_id}],
                "reason": "选项只决定接着问哪一项；具体取值要用户给出（不猜值）"},
            "report": (f"你选的是「{label}」—— 请给出具体内容，我按这一项继续"
                       f"（**还没有执行任何动作**）。")})
        return body

    def _pending_ask_body(self, request: Any, decision: Decision, *,
                          trace_id: str) -> dict[str, Any]:
        """追问 / 过期提示：**把候选念出来**（只说"请明确"等于没帮上忙）。"""
        pending = decision.pending
        question = decision.question or "你要确认的是哪一件事？"
        body = self._pending_base(request, trace_id=trace_id)
        body.update({
            "status": ChatStatus.CLARIFY.value,
            "clarification": {
                "questions": [{"slot": "confirm", "question": question,
                               "pending_id": pending.pending_id if pending else ""}],
                "reason": decision.reason or "确认对象需要用户明确"},
            "report": question})
        return body

    # ------------------------------------------------------------------
    def apply_pending_override(self, request: Any, body: dict[str, Any], result: Any,
                               resumed_from: str = "") -> bool:
        """越界兜底前的最后一道：会话里还有待确认 → **接回 pending**（不礼貌拒答）。

        ★ 判据三条都要：意图判成越界、会话里真有可匹配的待确认、**这一轮没有在接续旧任务**。
          没有待确认时照旧走越界拒答（那时这句话确实什么都不算）。
        ★ 不假装听懂：如实说"没听懂这句"，同时**不丢掉"我在等你确认"这件事**。
        ★ 返回"是否接手" —— 接手之后**不再登记新 pending**（否则会把等着的这条挤掉）。
        """
        state = getattr(result, "state", None)
        if getattr(state, "intent", None) is not Intent.OUT_OF_SCOPE:
            return False
        alive = self.pendings.candidates(request.session_id)
        if not alive:
            return False
        if resumed_from:
            # ★ 台账已经认出"这是在补上之前那件事" → **不许抢**（抢了就把正经接续变成一次追问）
            return False
        if not refers_to_pending(request.message, alive):
            # ★★ 这句**不像在回应待确认**（是另一个问题）→ 不接手，且把它**挂起**：
            #   修前这里会把新问题吞掉，回一句"我还等着你确认…"（用户的新问题一个字都没答）
            self._suspend_for_new_topic(request)
            return False
        question = ("这句我没听懂；不过我这边还等着你确认 "
                    + describe_candidates(alive)
                    + " —— 回「确认」我就按那条往下走，回「取消」就作废。")
        body.update({
            "status": ChatStatus.CLARIFY.value,
            "clarification": {
                "questions": [{"slot": "confirm", "question": question,
                               "pending_id": alive[0].pending_id}],
                "reason": "意图判为越界，但会话里有可匹配的待确认 → 交回待确认处理"},
            "report": question})
        body.setdefault("degraded", []).append(
            "越界输入按会话里的待确认状态接手（不把「还等着确认」这件事丢掉）")
        return True

    def _suspend_for_new_topic(self, request: Any) -> None:
        """用户说起了别的事 → 把还在等的待确认**挂起**（不删），并留痕。

        ★ 为什么是挂起而不是删：他很可能马上回来回一句「确认」；
          删掉等于把"我在等你确认"这件事永久丢掉（这正是确认类问题的老毛病）。
        """
        session_id = str(getattr(request, "session_id", "") or "")
        if not session_id:
            return
        alive = [item for item in self.pendings.candidates(session_id)
                 if item.status == "pending"]
        if not alive:
            return
        count = self.pendings.suspend_all(session_id)
        if count:
            self._new_topic_note = (
                f"先回答你这一句；会话里还有 {count} 条待确认已**挂起（没删）** —— "
                f"回「确认」/「取消」仍能接着办：{describe_candidates(alive)}")

    def _drain_new_topic_note(self, body: dict[str, Any]) -> None:
        """把"旧待确认已挂起"写进这一轮的 `degraded`（不静默）。"""
        note = getattr(self, "_new_topic_note", "")
        self._new_topic_note = ""
        if note:
            body.setdefault("degraded", []).append(note)

    # ------------------------------------------------------------------
    @staticmethod
    def _pending_base(request: Any, *, trace_id: str, incident_id: str = "",
                      intent_name: str = "") -> dict[str, Any]:
        """确认分支的响应体骨架（字段齐全）。

        ★ 追问 / 取消这类**过程性**响应**不占用** pending 指向的那个事件号 ——
          只有"命题确认"才是对那次结论的状态更新。否则追问的响应会把那份事故留存
          覆盖成一份没有根因的澄清（实测：二次确认之后再确认，结论就查不到了）。
        """
        return empty_contract_body(
            request.session_id, trace_id=trace_id, incident_id=incident_id,
            intent_name=intent_name, turn_id=ensure_turn_id(request),
            write_allowed=bool(getattr(request.options, "allow_write", False)))

    @staticmethod
    def _pending_sink(on_event: Optional[Callable[..., Any]], request: Any,
                      pending: Optional[PendingConfirmation],
                      *, trace_id: str) -> Optional[Callable[..., Any]]:
        """给 `tool` 事件补上 `trace_id` / `incident_id`（SSE 的硬性要求）。"""
        if on_event is None:
            return None
        incident_id = str((pending.target.get("incident_id") if pending else "") or "")
        return lambda event, data: on_event(event, {
            **data, "trace_id": trace_id, "incident_id": incident_id, "round": 0})
