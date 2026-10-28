"""澄清预算 —— `clarify` 的唯一出口。

--------------------------------------------------------------------------------
★ 为什么"问不问"必须由编排层说了算
--------------------------------------------------------------------------------

**`status=clarify` 只由编排层发出**。
理由有两个，缺一个都不成立：

1. **体验**：工具层/意图层各自反问，用户会被连环追问。多轮编排做完后这一点尤其危险 ——
   "每次取证失败都可能再问一遍"会演变成"你刚答完，它又问一遍"。
2. **可裁决**：问几次、问哪个缺口，是**预算问题**，而预算只有编排层看得见
   （意图层不知道这是第几轮、也看不到"这个缺口之前问过没有"）。

--------------------------------------------------------------------------------
★ 三条规则
--------------------------------------------------------------------------------

| 规则 | 实现 |
|---|---|
| 一次排查最多澄清 **N=2** 次 | `ClarifyBudget(max_questions=2)`；信息类问题用满就**降级继续** |
| 同一个问题只问一次 | 缺口集合来自工作记忆（`asked_gaps`，跨轮持久） |
| 判据不变、但叠一层预算 | 意图层的代价比较结果照用；它说"要问"而预算说"不能问"时，**预算赢**，并把不确定性写进结论 |

--------------------------------------------------------------------------------
★★ 破坏性动作的确认门**不占**信息预算
--------------------------------------------------------------------------------

`CONFIRM_ALWAYS` 要求回滚/扩容/重启**必须**有确认。它不是"再要一点信息"，
而是一道安全门。所以：

· 确认问题**永远不因为信息预算耗尽而被压掉**（压掉它 = 又一次 fail-open）；
· 但它记在**另一个**计数里（`confirmations`），不会把"补信息"的名额吃掉；
· 预算耗尽时的正确行为是"**不做动作**并说明需要确认"，不是"省一次追问就放行"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Mapping, Optional, Sequence

__all__ = ["ClarifyDecision", "ClarifyBudget", "DEFAULT_MAX_QUESTIONS", "CONFIRM_SLOT",
           "ACTION_SLOT", "REMEDIATION_SLOT", "SERVICE_SLOT", "EVIDENCE_SLOT",
           "UNCONFIRMABLE", "question_slot", "question_text", "question_dict",
           "merge_asked_gaps", "first_gap", "replace_unconfirmable_confirm",
           "evidence_gap_question"]

#: 一次排查最多澄清几次（经验值 N=2）
DEFAULT_MAX_QUESTIONS: Final[int] = 2

#: 确认类问题的槽位名（`ClarificationQuestion.slot`）—— 与 classifier 的约定
CONFIRM_SLOT: Final[str] = "confirm"

#: ★★ 「没认出具体动作」时替代确认门的槽位名与问法。
#:   真机现场（第 2→3 轮）：写意图但**没认出动作**时，问的是"你确认要执行处置动作吗？"
#:   —— 而**没有任何东西可以被确认**（`_pending` 是空的）→ 用户回「确认」→ 再问一遍同一句
#:   （**死循环**）。
#:   ★ 后来问的是"你要执行的是**哪个动作**" —— 那是**把选题推回给用户**：
#:     用户问「怎么解决」要的是**方案**，不是被反问"你想干什么"。
#:   ★ 现在：候选方案由代码生成（`remediation.plans`），这一问改成"你想选哪个方案"，
#:     并把候选摆成表格让用户挑 —— 只有**连目标服务都没有**时才追问服务。
REMEDIATION_SLOT: Final[str] = "remediation"

#: 兼容旧名：以前这个槽位叫 `action`。★ 台账与澄清白名单都按**新名**记账，
#:   于是"在等什么"如实写成"**在等用户选方案**"，而不是"在等 action 槽位"。
ACTION_SLOT: Final[str] = REMEDIATION_SLOT

SERVICE_SLOT: Final[str] = "service"

#: ★★ **还没有方案可选**时的缺口名：这时能说的只有"我先查什么"。
#:   与 `remediation`（"你想选哪个方案"）分开，因为两者的**前提不同** ——
#:   方案表还没出来时问"选哪个"，等于拿一句空话换另一句空话。
EVIDENCE_SLOT: Final[str] = "evidence"

#: 缺什么就问什么（**不确认一个拿不出来的动作**）：
#:   · 没认出动作 → 让用户在候选方案里挑一个；· 认出了动作但没认出对谁做 → 问服务
#:   （`_target_entity` 不猜服务名）。
UNCONFIRMABLE: Final[Mapping[str, tuple[str, str]]] = {
    REMEDIATION_SLOT: (
        REMEDIATION_SLOT,
        "你想选**哪个方案**？★ 处置建议已经按上面的表格给出（一行一个方案，含依据、"
        "预期、前置与回滚）—— 回「方案 2」或动作名（如「回滚」）即可，"
        "我会把**你所选的那条**细化成处置流程；★ 本阶段**不会执行**任何变更，"
        "仍要先过安全确认。"),
    SERVICE_SLOT: (
        SERVICE_SLOT,
        "要对**哪个服务**执行这个动作？"
        "★ 动作我认出来了，但**没认出对谁做**（不猜服务名）—— 所以这次**没有生成可落地的预案**；"
        "你点名服务后，我会先出预案与回滚方案、定风险等级，再等你确认。"),
    EVIDENCE_SLOT: (
        EVIDENCE_SLOT,
        "**这一轮还没查到可依据的证据，所以我暂时给不出处置方案**。"
        "我可以先查这件事相关的指标与日志（拿到**具体的错误类型**与拐点）再给方案 —— "
        "要我继续查吗？"),
}


def evidence_gap_question() -> dict[str, Any]:
    """"我还需要先查什么"那一问（**不是**"选哪个方案"）。"""
    slot, text = UNCONFIRMABLE[EVIDENCE_SLOT]
    return {"slot": slot, "question": text}


def replace_unconfirmable_confirm(questions: Sequence[Any], *,
                                  confirmable: Optional[bool],
                                  needs: str = ACTION_SLOT) -> tuple[Any, ...]:
    """★★ **没有该确认的东西**时，别问"回「确认」我才继续"。

    ★ 判据（`confirmable`）由编排层给，因为只有它知道"这一步到底是什么"：
      · `True` 且 `needs` 为空 → 有具体动作可确认 → **原样透传**（安全确认门该问就问）；
      · `True` 且 `needs` 为 `remediation` → **先让用户选方案**（确认门等选完再问）；
      · `False` → 写意图但**没有可确认的预案** → 把 `confirm` 问题换成"缺什么问什么"；
      · `None`  → 不是写意图 → **一律透传**（不插手其他意图的澄清）。

    ★ 为什么这是"如实"而不是"降级"：问"要不要执行"却拿不出可执行的东西，
      等于**承诺了做不到的事**（用户回了确认，我什么也确认不了 —— 真机实测会**死循环**）；
      而候选方案还没选定时问确认门，用户看到的是"确认哪一个？"（一次只能确认一个动作）。
    ★ 安全门**没有任何放宽**：没有可执行的东西，本来就无从 fail-open。
    """
    if confirmable is None:
        return tuple(questions)
    if confirmable and not needs:
        return tuple(questions)
    slot, text = UNCONFIRMABLE.get(needs, UNCONFIRMABLE[ACTION_SLOT])
    if slot in {question_slot(q) for q in questions
                if question_slot(q) != CONFIRM_SLOT}:
        # ★ 那个缺口**已经在问了**（例如缺服务时，服务插件本来就有一问）→
        #   只把"接不住的确认"去掉，**不重复问同一件事**（同一句话问两遍只会让用户烦）。
        return tuple(q for q in questions if question_slot(q) != CONFIRM_SLOT)
    out: list[Any] = []
    for question in questions:
        if question_slot(question) != CONFIRM_SLOT:
            out.append(question)
            continue
        item = question_dict(question)
        item.update({"slot": slot, "question": text, "replaced_confirm": True})
        out.append(item)
    return tuple(out)


def question_slot(question: Any) -> str:
    """取一个候选问题的 `slot`（**同时支持 dataclass 与 dict**）。

    ★ 为什么两种都要支持：意图层内部用 `ClarificationQuestion`（dataclass），
      而它进 `IntentResult.clarification` 时**已经变成 dict**（契约字段是
      `Optional[dict]`）。编排层拿到的通常是后者 —— 只认一种就会静默认为
      "没有问题"，于是澄清永远不发生（落地时真踩到）。
    """
    if isinstance(question, Mapping):
        return str(question.get("slot") or "")
    return str(getattr(question, "slot", "") or "")


def question_text(question: Any) -> str:
    if isinstance(question, Mapping):
        return str(question.get("question") or "")
    return str(getattr(question, "question", "") or "")


def question_dict(question: Any) -> dict[str, Any]:
    if isinstance(question, Mapping):
        return dict(question)
    to_dict = getattr(question, "to_dict", None)
    return dict(to_dict()) if callable(to_dict) else {"question": str(question)}


@dataclass(frozen=True)
class ClarifyDecision:
    """编排层对"要不要问"的最终裁决（**唯一出口**）。"""

    ask: bool
    questions: tuple[Any, ...] = ()          # 候选问题原样透传（dict 或 dataclass）
    reason: str = ""
    asked_gaps: tuple[str, ...] = ()         # 这次真正问出去的缺口（写回工作记忆）
    suppressed: tuple[str, ...] = ()         # 被预算压掉的缺口（要写进降级说明）
    confirmations: tuple[str, ...] = ()      # 确认类问题（不占信息预算）
    requires_confirmation: bool = False      # 保留意图层的"这是动作确认门"标记

    def to_dict(self) -> dict[str, Any]:
        return {
            "ask": self.ask,
            "questions": [question_dict(q) for q in self.questions],
            "reason": self.reason,
            "asked_gaps": list(self.asked_gaps),
            "suppressed": list(self.suppressed),
            "confirmations": list(self.confirmations),
            "requires_confirmation": self.requires_confirmation,
        }

    def to_payload(self) -> Optional[dict[str, Any]]:
        """转成响应里 `clarification` 的形状（与 `IntentResult.clarification` 对齐）。"""
        if not self.ask or not self.questions:
            return None
        payload: dict[str, Any] = {
            "questions": [question_dict(q) for q in self.questions],
            "reason": self.reason,
        }
        if self.suppressed:
            # ★ 压掉了什么必须说出来，否则用户不知道"还有问题没问"
            payload["suppressed"] = list(self.suppressed)
        if self.requires_confirmation:
            payload["requires_confirmation"] = True
        return payload


@dataclass
class ClarifyBudget:
    """澄清的次数预算 + 已问缺口（内存视图；权威记账在工作记忆里）。"""

    max_questions: int = DEFAULT_MAX_QUESTIONS
    asked: set[str] = field(default_factory=set)          # 本次排查里问过的缺口
    confirmations_asked: int = 0

    def decide(self, questions: Sequence[Any], *,
               already_asked: Sequence[str] = (),
               requires_confirmation: bool = False) -> ClarifyDecision:
        """按预算裁决候选问题。

        Args:
            questions: 意图层给出的候选问题（`ClarificationQuestion` **或** dict）。
            already_asked: **跨轮**问过的缺口（工作记忆 `asked_gaps`）。
            requires_confirmation: 意图层标记的"这是动作确认门"（原样保留到响应）。
        """
        history = set(self.asked) | {str(g) for g in already_asked}
        confirm: list[Any] = []
        info: list[Any] = []
        for question in questions:
            (confirm if question_slot(question) == CONFIRM_SLOT else info).append(question)

        kept: list[Any] = []
        kept_gaps: list[str] = []
        suppressed: list[str] = []
        for question in info:
            gap = question_slot(question) or question_text(question)
            if gap and gap in history:
                suppressed.append(f"{gap}（本次会话已经问过）")
                continue
            if len(kept) >= max(0, self.max_questions):
                suppressed.append(f"{gap}（澄清预算已用满 {self.max_questions} 次）")
                continue
            kept.append(question)
            if gap:
                kept_gaps.append(gap)

        # ★ 确认类**不受信息预算约束**（安全门），但单独计数
        if confirm:
            self.confirmations_asked += 1
        self.asked.update(kept_gaps)
        self.asked.update(CONFIRM_SLOT for _ in confirm)

        final = list(confirm) + kept
        if not final:
            reason = ("澄清预算已用满/缺口都问过 → **不再打断用户**，"
                      "改为带着不确定性继续"
                      + (f"（压掉：{'; '.join(suppressed)}）" if suppressed else ""))
        else:
            parts = []
            if confirm:
                parts.append(f"{len(confirm)} 个破坏性动作确认（不占信息预算）")
            if kept:
                parts.append(f"{len(kept)} 个缺信息问题（额度 {self.max_questions}）")
            reason = "编排层允许澄清：" + "、".join(parts)
            if suppressed:
                reason += f"；压掉 {len(suppressed)} 个（{'; '.join(suppressed)}）"
        return ClarifyDecision(ask=bool(final), questions=tuple(final), reason=reason,
                              asked_gaps=tuple(kept_gaps), suppressed=tuple(suppressed),
                              confirmations=tuple(question_slot(q) for q in confirm),
                              requires_confirmation=bool(requires_confirmation))

    def snapshot(self) -> dict[str, Any]:
        return {"max_questions": self.max_questions, "asked": sorted(self.asked),
                "confirmations_asked": self.confirmations_asked}


def merge_asked_gaps(*groups: Sequence[str]) -> set[str]:
    """把多来源的"已问缺口"合成一个集合（工作记忆 + 本次排查）。"""
    out: set[str] = set()
    for group in groups:
        out.update(str(item) for item in (group or ()) if str(item).strip())
    return out


def first_gap(decision: ClarifyDecision) -> Optional[str]:
    """这次问出去的第一个缺口（写工作记忆用）。"""
    return decision.asked_gaps[0] if decision.asked_gaps else None
