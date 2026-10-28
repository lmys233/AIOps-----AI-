"""写意图的**处置建议**：候选方案 → 用户选择 → 变更预案 + 风险分级。

★ 本模块的方法原样搬自 `orchestrator.py` 的 `Orchestrator`，
  作为 mixin 参与 `Orchestrator` 的组装（**没有第二份实现**）。

--------------------------------------------------------------------------------
★★ 为什么"没认出动作"不再反问动作
--------------------------------------------------------------------------------

写意图以前只走一条路：从用户原话里认出**单一**动作（`tools.risk.match_action`），
认不出就把"确认"换成"你要执行的是哪个动作" —— 用户问「怎么解决」要的是**方案**，
却把选题推回给了他，而且一次只能给一个方案。

现在分三条路：

| 情形 | 处置 |
|---|---|
| 用户的话**指到了**某条候选方案（「方案 2」/「回滚」） | 针对**该方案**出流程 + 仍需安全确认（依然**不执行**） |
| 认出了**单一**动作 | 保持原行为：单一预案 + 分级 + 确认门（一个字都没放宽） |
| 没认出动作 | **生成候选方案**写进 `flags["remediation_plans"]` → 表格/流程给用户挑 |

★ 候选由**代码**生成（`remediation.plans`），白名单外与 L4 的动作一律不出现。
★ 安全门一动没动：本阶段没有任何写通道，方案一律不执行。
"""

from __future__ import annotations

from typing import Any, Sequence

from ..common.enums import ActionClass, Intent, RiskLevel
from ..common.steps import emit_step
from ..llm.gateway import LLMError
from ..remediation.ai_plans import (PLAN_MIN_TOKENS, PLAN_ROLE, PlanDraftSet,
                                    build_plan_prompt, evidence_ids,
                                    plans_from_drafts, render_evidence_block)
from ..remediation.gaps import anchored_to, evidence_gap_plan
from ..remediation.plans import (DEFAULT_PLAN_LIMIT, RemediationPlan, candidate_plans,
                                 plan_from_ledger, plan_steps_for, select_plan)
from .evidence_scope import (SOURCE_CURRENT, SOURCE_UNKNOWN, anchoring_names,
                             inherited_at_text, inherited_record, scope_evidence,
                             service_names_of)
from .state import GraphState

from ..tools.risk import ActionMatch, classify_risk, match_action, propose_action

#: 从编排状态里能读到的**变更事件**的兜底键（真环境由 Change Agent 写入）
_CHANGE_FLAGS: tuple[str, ...] = ("changes", "change_events")


def _no_evidence_reason(names: Sequence[Any], scoped: Sequence[Any]) -> str:
    """一条方案都提不出来时，**如实说清原因**（`action_blocked` 这一栏用户看得见）。"""
    where = "、".join(str(name) for name in (names or ()) if str(name).strip()) \
        or "用户点名的那件事"
    if scoped:
        return f"本轮有 {len(scoped)} 条证据，但没有一条支撑得起可落地的变更预案"
    return (f"本轮**没有针对 {where} 取到证据**（也没有对得上的上一轮证据）→ "
            f"**不构造变更预案**：零证据下给动作就是在编；先取证再定处置")


def _proposal_would_block(state: GraphState, match: ActionMatch) -> bool:
    """这次识别出来的动作，`propose_action` 会不会因为**没认出目标**而拦下。"""
    return not propose_action(match, slots=state.slots).ok


def _scoped_hypotheses(hypotheses: Sequence[Any],
                       wanted: frozenset[str]) -> tuple[Any, ...]:
    """只留**主语落在锚定服务上**的假设（方案不该拿别的服务的根因当依据）。

    ★ 真机实测：「怎么解决 orders 工作线程池已满」那一轮，假设里主语的类别是
      `error_burst`（打分框架给 inventory 的判读），照它生成方案就会给出
      "确认 excluded_reason 是否存在、修对那条 SQL" —— 与用户问的事无关。
    ★ 空 `wanted` = 不筛（不知道就别筛）。
    """
    if not wanted:
        return tuple(hypotheses or ())
    kept: list[Any] = []
    for item in hypotheses or ():
        root = getattr(item, "root_entity", None)
        name = str(getattr(root, "name", "") or "").strip().lower()
        if not name or name in wanted:
            kept.append(item)
    return tuple(kept)


def proposal_blocked_for_target(state: GraphState) -> bool:
    """用户点名的动作**认得出、但认不出对谁做**（判"该不该问服务"）。

    ★ 与 `_prepare_action` 里那条拦点**同一个判据**（`propose_action`），
      两处各写一份必然漂移：一处问服务、另一处却把动作提出来了。
    """
    match = match_action(state.user_message, service_names=_names_for(state))
    return bool(match.found) and _proposal_would_block(state, match)


def _names_for(state: GraphState) -> list[str]:
    slot = getattr(state.slots, "service", None)
    names = list(getattr(state.slots, "service_names", None) or ())
    if slot is not None and getattr(slot, "name", ""):
        names.insert(0, str(slot.name))
    return names


class ActionPlanMixin:
    """写意图的**处置建议 + 变更预案 + 风险分级**。"""

    # ------------------------------------------------------------------
    #  ★★ 方案生成（模型写草稿，代码收口）
    # ------------------------------------------------------------------
    async def _generate_ai_plans(self, state: GraphState) -> None:
        """把**三层记忆 + 本轮取证事实**交给模型生成方案草稿，交代码收口。

        ★ 为什么方案要模型写：对症与否取决于"**这类问题以前怎么处置的**"
          （程序性记忆的 SOP、情景记忆的既往案例）与"这一轮到底查到了什么"——
          这两样只有模型读得到。生成之后**收口全在代码侧**（白名单 / 风险等级 /
          L4 丢弃 / 引用校验 / 机制族对症），见 `remediation.ai_plans`。
        ★ 零证据**不生成**（连草稿都不写）：没有证据的方案就是编。
        ★ 记忆是**增强**：召回不可读时照常按证据生成，并在留痕里写明这次没有记忆 ——
          绝不因为记忆不可读而取消方案或改动判据。
        """
        if state.intent is not Intent.REMEDIATION or not state.evidence:
            return
        if state.flags.get("ai_plans") is not None:
            return                     # 这一轮已经生成过（一次请求只花一次模型）
        gateway = getattr(self.router, "gateway", None)
        if gateway is None:
            return
        names = self._service_names(state)
        scoped = self._scoped_evidence(
            state, anchoring_names(names, self._evidence_names(state)))
        if not scoped:
            return                     # 证据不在点名服务上 → 由"先取证"那条路如实收口
        memory_block, memory_note = await self._plan_memory(state)
        prompt = build_plan_prompt(
            message=state.user_message, services=names,
            evidence_block=render_evidence_block(scoped),
            memory_block=memory_block, context_block=await self._context_block(state))
        from ..attribution.v2.presentation import plan_context
        prompt += plan_context(state.flags.get('attribution_v2'))
        drafts = await self._plan_drafts(state, prompt)
        if drafts is None:
            return
        plans, notes = plans_from_drafts(
            drafts, evidence=scoped, message=state.user_message, services=names,
            evidence_source=SOURCE_CURRENT)
        state.flags["ai_plans"] = plans
        self._remember_plan_source(state, plans, scoped, [
            *([memory_note] if memory_note else []), *notes])
        if not plans:
            state.add_note("模型没给出能收口的方案 → 退回既有候选生成器")

    def _remember_plan_source(self, state: GraphState, plans: Sequence[RemediationPlan],
                              scoped: Sequence[Any], notes: Sequence[str]) -> None:
        """记下"这一次方案是怎么来的"（进 `plans` 那步的留痕与降级说明）。"""
        state.flags["ai_plan_step"] = {
            "source": "ai", "evidence_ids": list(evidence_ids(scoped)),
            "plans": len(plans), "notes": list(notes)[:4]}
        for note in notes:
            state.add_note(f"方案生成：{note}")
            state.degraded_detail.append(f"方案生成：{note}")

    async def _plan_drafts(self, state: GraphState, prompt: str) -> list[Any] | None:
        """喊一次模型写方案草稿；失败一律**如实降级**（返回 `None` → 走代码候选）。"""
        gateway = getattr(self.router, "gateway", None)
        model = str(getattr(self.router, "main_model", "") or "") or None
        if self.attribution_v2_enabled:
            from ..attribution.v2.limits import model_limits
            settings=model_limits(state,prompt,PlanDraftSet,PLAN_MIN_TOKENS)
            if settings is None:return None
        else:settings={'max_tokens':PLAN_MIN_TOKENS}
        try:
            from ..common.model_budget import model_budget_scope
            from contextlib import nullcontext
            with model_budget_scope(state.budget) if self.attribution_v2_enabled else nullcontext():
                if self.attribution_v2_enabled:
                    from ..attribution.v2.limits import bounded_generate
                    raw=await bounded_generate(gateway,state.budget,prompt,PlanDraftSet,
                        role=PLAN_ROLE,model=model,temperature=0.2,**settings)
                else:
                    raw=await gateway.generate(prompt,PlanDraftSet,role=PLAN_ROLE,
                        model=model,temperature=0.2,**settings)
        except LLMError as exc:
            state.add_note(f"方案生成失败（{exc.code}）→ 退回既有候选生成器")
            state.degraded_detail.append(f"方案生成失败（{exc.code}: {exc}）→ 方案由代码候选给出")
            return None
        except Exception as exc:      # noqa: BLE001 —— 方案是增强，失败不该带走这一轮
            state.add_note(f"方案生成异常（{type(exc).__name__}）→ 退回既有候选生成器")
            state.degraded_detail.append(
                f"方案生成异常（{type(exc).__name__}: {exc}）→ 方案由代码候选给出")
            return None
        return list(getattr(raw, "plans", None) or [])

    async def _plan_memory(self, state: GraphState) -> tuple[str, str]:
        """方案要用的**长期记忆**（SOP + 既往案例）：`(注入块, 留痕)`。

        ★ 近/中/远三层的会话上下文由 `_context_block` 给（原文 / 摘要 / 索引）；
          这里补的是跨会话的两层 —— **程序性记忆**（这类问题的标准处置）与
          **情景记忆**（以前处置过什么、结果如何）。方案对不对症主要看这一份。
        ★ 原文**按需回取**：注入的是有界的摘要，每条带 `memory_id`（回取的钥匙），
          不把历史全文搬进提示词。
        """
        memory = getattr(self, "memory", None)
        if memory is None:
            return "", "未接入长期记忆 → 本次方案只依据本轮证据"
        symptom = str(getattr(state.slots, "symptom", "") or "") or state.user_message
        try:
            result = await memory.recall_detailed(slots=state.slots, symptom=symptom,
                                                  top_k=DEFAULT_PLAN_LIMIT)
        except Exception as exc:      # noqa: BLE001 —— 记忆不可读只是少一层输入
            return "", (f"长期记忆召回失败（{type(exc).__name__}）→ "
                        f"本次方案只依据本轮证据")
        block = str(getattr(result, "injection", "") or "").strip()
        if not block:
            return "", "长期记忆没有命中相似案例 → 本次方案只依据本轮证据"
        refs = [str(getattr(hit, "memory_id", "") or "")
                for hit in (getattr(result, "hits", ()) or ())]
        refs = [ref for ref in refs if ref][:5]
        note = (f"已注入记忆：召回 {len(refs)} 条（SOP/既往案例，可回取 id "
                f"{'、'.join(refs)}）")
        return f"{block}\n（以上条目的原文可按 id 回取：{'、'.join(refs)}）", note

    def _prepare_action(self, state: GraphState) -> None:
        """`REMEDIATION` 时给出**处置建议**（候选方案或单一预案）。

        ★ 认动作走**规则**（`tools.risk.match_action`），不走模型：这条路径直连写能力，
          让模型决定 `action_class` 等于给它机会把 L4 说成 L2。
        ★ 这里**只出方案与预案**：执行端（GitOps）在后续阶段，本阶段只做
          "给候选 / 分级 / 要不要人批"。

        ★★ **零证据不出方案**：这一轮既没有冲着点名服务取到的证据、上一轮也没有
          **对得上**的证据时，候选里只留一条"先补齐证据"的如实建议 ——
          给一条"看起来可行"的方案（真机实测：问 order 线程池，答 inventory 的 SQL）
          比说"我还需要查什么"更糟：它会把人往错的方向带。
        """
        if state.intent is not Intent.REMEDIATION:
            return
        state.flags.pop("evidence_gap_pending", None)
        if state.flags.pop("remediation_finalized", False):
            return                    # 这一轮已经按**本轮证据**定稿过，不再重算
        names = self._service_names(state)
        wanted = anchoring_names(names, self._evidence_names(state))
        scoped, hypotheses, source = self._scoped_for_plans(state, wanted)
        plans = self._anchored_plans(state, scoped, hypotheses, wanted, source=source)
        # 选择只能指向台账中实际展示过的候选，不从本轮新算的方案里猜。
        # 一般修复请求、时间窗数字和短词重合都不构成用户选择。
        state.flags.pop("selected_plan", None)
        picked = plan_from_ledger(tuple(state.flags.get("active_tasks") or ()), state.user_message)
        if picked is not None and wanted and not anchored_to(picked, wanted):
            picked = None
        match = match_action(state.user_message, service_names=names)
        # ★★ `dry_run_only` 的拦点**必须在"有没有证据"之前**：它是客户端下的
        #   硬要求（不许产出可落地的变更预案），不能被"这一轮没取证"提前绕过去。
        dry_run = (match.found and state.flags.get("dry_run_only")
                   and not _proposal_would_block(state, match))
        if not plans and not dry_run:
            plans = self._record_evidence_gap(state, names, scoped, match)
        state.flags["remediation_plans"] = plans
        state.flags["remediation_evidence_source"] = source
        self._emit_plans_step(state, scoped, hypotheses, wanted, source, plans)
        self._settle_plans(state, picked, match, plans)
        # ★★ 按**本轮**证据定稿 → 记一次；`report` 节点不再重算（避免同一件事算两遍）
        if scoped and source == SOURCE_CURRENT:
            state.flags["remediation_finalized"] = True

    def _settle_plans(self, state: GraphState, picked: Any,
                      match: ActionMatch, plans: Sequence[RemediationPlan]) -> None:
        """这一轮**落到哪一条**：用户选的 / 零证据占位 / 认出的单一动作 / 候选表。

        ★ 抽出来是为了让 `_prepare_action` 只管"算出候选"，"怎么收口"单独一处 ——
          四条分支各有一句实话要说，混在算候选的那段里读不出重点。
        """
        if picked is not None:
            self._selected_plan(state, picked)
            return
        if match.found and state.flags.get("evidence_gap_pending"):
            # ★★ 认出了动作、但本轮**没有对得上的证据** → **不构造变更预案**。
            #   真机实测的靶子就是这样来的：零证据也照样落一份预案（`proposed_action`），
            #   于是这一轮的确认门与方案表互相矛盾 —— 方案表说"先取证再定处置"，
            #   预案却说"要重启 order"。动作没被否掉，只是要等取证之后
            #   （`report` 节点会按**本轮证据**重跑 `_prepare_action`，那条路一个字没动）。
            state.add_note(
                f"认出了动作 {match.action_class.value}，但本轮没有冲着点名的服务取到证据 → "
                f"**不构造变更预案**（零证据下给动作就是在编）：先取证、再定处置")
            return
        if match.found:
            self._proposal(state, match)
            return
        self._note_candidates(state, tuple(plans))

    # ------------------------------------------------------------------
    def _scoped_for_plans(self, state: GraphState, wanted: frozenset[str]
                          ) -> tuple[tuple[Any, ...], tuple[Any, ...], str]:
        """算方案可依据的那批证据：**本轮取到的**，取不到才**继承上一轮**（并标注出处）。"""
        scoped = self._scoped_evidence(state, wanted)
        if scoped:
            return scoped, tuple(state.hypotheses), SOURCE_CURRENT
        scoped, hypotheses = self._inherit_investigation(state, wanted)
        source = (f"上一轮（{inherited_at_text(state.flags) or '时刻未记录'}）"
                  if scoped else SOURCE_UNKNOWN)
        return scoped, hypotheses, source

    def _record_evidence_gap(self, state: GraphState, names: Sequence[str],
                             scoped: Sequence[Any], match: ActionMatch
                             ) -> tuple[RemediationPlan, ...]:
        """一条**真实**候选都没有 → 只留占位（"先补齐证据"），并如实记下原因。

        · 认出了动作 + 认出了目标 → 动作本身没被否掉，只是**还没有证据**
          （记 `evidence_gap_pending`，取证后照旧走 `_proposal`，安全门一个字不动）；
        · 其余（没认出动作 / 没认出对谁做）→ 记 `action_blocked`，说清为什么。
        """
        state.add_note(
            "★ 这一轮**没有冲着" + "、".join(names or ("（未定服务）",))
            + "取到证据**，上一轮也没有对得上的 → 不出处置方案："
              "零证据的\"看起来可行\"等于编，先把证据补齐再定处置")
        reason = _no_evidence_reason(names, scoped)
        if match.found and not _proposal_would_block(state, match):
            state.flags["evidence_gap_pending"] = reason
            # ★ 动作认出来了、也能认出对谁做 —— 只是**还没有证据**。
            #   这一条原因必须**用户看得见**（`action_blocked` 是响应里那一栏），
            #   否则用户只看到 `proposed_action: null`，不知道是"没证据"还是"被拦了"。
            state.flags["action_blocked"] = reason
            state.add_note(reason)
        else:
            state.flags["action_blocked"] = reason
            state.add_note(reason)
        return (evidence_gap_plan(names),)

    def _emit_plans_step(self, state: GraphState, scoped: Sequence[Any],
                         hypotheses: Sequence[Any], wanted: frozenset[str],
                         source: str, plans: Sequence[RemediationPlan]) -> None:
        """方案生成也是一次"工具调用"（契约修订 20）：算了几条、依据是什么，如实摆出来。

        ★★ `source` 是**这一步的事实**（模型生成 / 代码候选），`evidence_ids` 是这次方案
          引用到的那几条本轮证据 —— 用户与审核都要能一眼看出"方案是拿什么算出来的"。
        """
        step = dict(state.flags.pop("ai_plan_step", None) or {})
        origin = str(step.get("source") or "code")
        revision = int(state.flags.get("plans_step_revision") or 0) + 1
        state.flags["plans_step_revision"] = revision
        emit_step(lambda event, data: self._emit(state, event, data), "plans", "生成处置方案",
                  {"evidence": len(scoped), "hypotheses": len(hypotheses),
                   "evidence_source": source, "origin": origin,
                   "evidence_ids": list(step.get("evidence_ids") or evidence_ids(scoped)),
                   "mechanisms": [plan.mechanism for plan in plans[:3] if plan.mechanism],
                   "scope": "、".join(sorted(wanted)) or "（未定）",
                   "deployment_form": str(state.flags.get("deployment_form") or "")},
                  result=(f"{'模型生成' if origin == 'ai' else '代码候选'} {len(plans)} 条："
                          + "、".join(f"{plan.kind}/{plan.action_class or '人工'}"
                                     for plan in plans[:3])
                          if plans else "本轮没有候选方案（证据不足 → 先补齐证据）"),
                  call_id=f"step-plans-{state.round}-{revision}")

    # ------------------------------------------------------------------
    def _anchored_plans(self, state: GraphState, evidence: Sequence[Any],
                        hypotheses: Sequence[Any], wanted: frozenset[str], *,
                        source: str = SOURCE_CURRENT) -> tuple[RemediationPlan, ...]:
        """候选方案 **∩ 用户点名的那件事**；没有对得上的就返回空（调用方给"补齐证据"）。

        ★★ **模型生成的方案优先**：它是在**锚定之后的证据**上生成、并按机制族筛过的
          （见 `_generate_ai_plans`）—— 这里不再按字面锚定，因为机制说明里未必逐字出现
          服务名，字面锚定会把最对症的那一条丢掉。代码候选那条路一个字没动。
        ★ 匹配是**对称**的：方案的动作目标落在点名服务上（`order`），或题目里
          点到的服务出现在方案依据里，二者有一个成立就算锚住。
        ★ 没有任何可依据的证据时也不出方案 —— 那时"候选"只是一堆通用动作。
        ★ `source` 如实写进「依据」：继承来的证据标成**上一轮（时刻）**，不伪装成本轮。
        """
        ai = tuple(state.flags.pop("ai_plans", ()) or ())
        if ai:
            return ai
        if not evidence and not hypotheses:
            return ()
        hypotheses = _scoped_hypotheses(hypotheses, wanted)
        plans = candidate_plans(message=state.user_message, hypotheses=hypotheses,
                                evidence=evidence, changes=self._changes_of(state),
                                services=self._service_names(state),
                                deployment_form=str(
                                    state.flags.get("deployment_form") or ""),
                                evidence_source=source)
        if not wanted:
            return plans
        return tuple(plan for plan in plans if anchored_to(plan, wanted))

    def _evidence_names(self, state: GraphState) -> list[str]:
        """**这一轮**取到的证据点到的服务名（用来把锚定并上"证据里真的有"）。"""
        names: set[str] = set()
        for item in state.evidence:
            names |= service_names_of(item)
        # ★ 点名的服务也进锚（中文俗称与契约里的服务名可能不是同一个字面）
        for name in self._service_names(state):
            if name:
                names.add(str(name).strip().lower())
        return sorted(names)

    def _scoped_evidence(self, state: GraphState,
                         wanted: frozenset[str]) -> tuple[Any, ...]:
        """这一轮**冲着点名服务**取到的证据（没有就是空）。"""
        return tuple(scope_evidence(tuple(state.evidence), wanted))

    # ------------------------------------------------------------------
    def _inherit_note(self, state: GraphState, text: str) -> None:
        """继承/不继承的说明要**同时**进 `notes` 与 `degraded_detail`。

        ★ `notes` 只进 `explanation`（折叠区）；用户真正能看到的通道是 `degraded`，
          而它读的是 `state.degraded_detail` —— 只写一处等于"留痕没人看得见"。
        """
        state.add_note(text)
        if text not in state.degraded_detail:
            state.degraded_detail.append(text)

    def _inherit_investigation(self, state: GraphState,
                               wanted: frozenset[str]) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        """本轮没有证据时，取**上一轮那次排查**的证据与假设（并如实留痕）。

        ★ 只在**实体对得上**时继承：上一轮查的是 inventory、这一轮问的是 payment 时继承，
          等于把别人的证据安到这次头上（那是"编"）。对不上就**明说为什么不继承**。
        ★ 不把继承来的证据塞进 `state.evidence`：这一轮确实没有取证，
          写进去会让答案声称"本次查到"—— 只用于**生成方案**，并在留痕里标明出处。
        """
        carried = inherited_record(state.flags)
        if not carried.get("evidence"):
            return (), ()
        incident_id = str(carried.get("incident_id") or "")
        source_names = {str(item).strip().lower()
                        for item in (carried.get("services") or ()) if str(item).strip()}
        if source_names and wanted and not (source_names & wanted):
            self._inherit_note(
                state,
                f"上一轮排查的是 {'、'.join(sorted(source_names))}、这次问的是 "
                f"{'、'.join(sorted(wanted))} → **不继承**它的证据（避免把别人的证据安到这次头上）")
            return (), ()
        evidence = tuple(scope_evidence(carried.get("evidence") or (), wanted))
        hypotheses = _scoped_hypotheses(carried.get("hypotheses") or (), wanted)
        age = float(carried.get("age_sec") or 0.0)
        when = inherited_at_text(state.flags)
        self._inherit_note(
            state,
            f"★ 本轮**没有重新取证** → 处置候选**继承**上一轮那次排查（事故 `{incident_id}`，"
            f"抓取时刻 {when or '未记录'}，约 {age:.0f} 秒前的 {len(evidence)} 条证据 / "
            f"{len(hypotheses)} 个假设）；方案里的依据标注为**上一轮（{when or '时刻未记录'}）**"
            f"的证据，不是本轮的")
        return evidence, hypotheses

    # ------------------------------------------------------------------
    @staticmethod
    def _service_names(state: GraphState) -> list[str]:
        """已知的服务名（槽位里确认过的那个排最前）。"""
        names = list(state.slots.service_names or ())
        if state.slots.service is not None:
            names.insert(0, state.slots.service.name)
        return names

    @staticmethod
    def _changes_of(state: GraphState) -> tuple[Any, ...]:
        """窗口内的变更事件（编排层留了就用，没有就是空 —— 不猜"有变更"）。"""
        for key in _CHANGE_FLAGS:
            found = state.flags.get(key)
            if found:
                return tuple(found)
        return ()

    def _selected_plan(self, state: GraphState, plan: RemediationPlan) -> None:
        """用户在这一轮**选定了**某条方案 → 把它细化成处置流程（**仍不执行**）。

        ★ 人工方案（`manual`）**不落** `flags["action"]`：Agent 没有写通道，
          给它配一个动作就是假装配得上能执行（如实原则）。
        ★ 选中 `agent_action` 方案时仍然走既有的风险分级与确认门，安全门一动不动。
        """
        state.flags["selected_plan"] = plan
        if plan.kind == "agent_action" and plan.action_class:
            try:
                action_class = ActionClass(plan.action_class)
            except ValueError:                 # 白名单外（构造期自检已挡住，这里是兜底）
                state.add_note(f"方案 {plan.option_id} 的动作不在白名单里 → 只给流程")
            else:
                names = self._service_names(state)
                self._proposal(state, ActionMatch(action_class=action_class, keyword="",
                                                  target_name=names[0] if names else ""))
        flow = " -> ".join(plan_steps_for(plan))
        state.add_note(
            f"用户选定处置方案：{plan.title}（风险 {plan.risk_level}"
            f"{'，需人工审批' if plan.requires_approval else ''}）→ 处置流程：{flow}")

    def _note_candidates(self, state: GraphState, plans: tuple[RemediationPlan, ...]) -> None:
        """没认出动作 → 如实说明**候选方案**（这就是给用户的答复，不再反问动作）。"""
        if not plans:
            state.add_note("写意图但没认出具体动作，也没有可依据的证据 → 只出澄清，不构造预案")
            return
        summary = "、".join(f"方案 {plan.option_id}·{plan.title}（{plan.risk_level}）"
                            for plan in plans)
        state.add_note(
            f"没认出具体动作 → 生成 {len(plans)} 个候选方案（{summary}）；"
            f"用户回「方案 N」或动作名即把它细化成处置流程（本阶段仍不执行）")

    def _proposal(self, state: GraphState, match: ActionMatch) -> None:
        """认出了**单一**动作 → 保持原行为：预案 + 分级 + （必要时）确认门。"""
        proposal = propose_action(match, slots=state.slots)
        if not proposal.ok:
            # ★★ 认出了动作但**没认出对谁做** → 不猜一个服务名，
            #   如实说明"需要先确认目标"，并留一条 flag 让响应里能看出来。
            state.add_note(f"不构造变更预案：{proposal.blocked_reason}")
            state.flags["action_blocked"] = proposal.blocked_reason
            return
        action = proposal.action
        # ---- ★★ `options.dry_run_only` 的**真读取点** ----
        #   要求"只许干跑"时，识别出的真实变更（起点等级 > L1）**不产出可落地预案**：
        #   风险分级照样给（用户要看得到"这本该是 L2"），但不进审批链、不留待执行动作。
        if state.flags.get("dry_run_only") and action.default_level not in (
                RiskLevel.L0.value, RiskLevel.L1.value):
            risk = classify_risk(action, mentioned=state.user_message)
            reason = (f"客户端要求 `dry_run_only=true` → 不生成可落地的变更预案："
                      f"「{action.action_class.value} @ {action.target.id}」"
                      f"本该是 {risk.level.value}（需审批={risk.requires_approval}），"
                      f"本次只分级、不落地")
            state.add_note(reason)
            state.flags["action_blocked"] = reason
            state.flags["risk"] = risk
            return
        risk = classify_risk(action, mentioned=state.user_message)
        state.flags["action"] = action
        state.flags["risk"] = risk
        state.add_note(f"变更预案：{action.action_class.value} @ {action.target.id}"
                       f"（风险 {risk.level.value}，需审批={risk.requires_approval}）")
