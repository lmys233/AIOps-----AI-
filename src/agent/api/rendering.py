"""把（状态 + 结构化事实）**渲染**成响应体（唯一渲染入口）。"""

from __future__ import annotations

from typing import Any, Final, Optional, Sequence
from datetime import datetime, timezone
import re
from ..attribution.problems import problem_rows
from ..common.enums import Intent
from .contract_body import empty_contract_body
from .idempotency import ensure_turn_id
from .requests import ChatRequest
from .answer import missing_identifiers, template_answer
from ..orchestration.constants import EVIDENCE_INTENTS, LIGHT_INTENTS
from ..remediation.plans import RemediationPlan, format_plans, selected_from_flags
from .export import ADVISOR_NOTE
from .advisor import advisor_outcome

#: 走**答案总结**（白话/模型）的状态 —— 其余状态用固定话术（它们本身就是
#:   给用户看的话术，例如澄清、越界、知识问答）。
_ANSWER_STATUSES: Final[frozenset[str]] = frozenset(
    # ★ 契约修订 21：`answered`（值问答给出了结果）同样要走白话答案那条路
    {"abstained", "answered", "concluded", "budget_exhausted"})

#: 这些意图的 `report` **已经是**给用户看的答案（越界拒答 / 知识问答的降级说明），
#: 不该被"再总结一次"覆盖掉 —— 覆盖会丢掉它们要传达的关键信息。
_TAILORED_ANSWER_INTENTS: Final[frozenset[Intent]] = frozenset(
    {Intent.OUT_OF_SCOPE})

#: 单方案时的说法（流程紧随其后）
_PLAN_FLOW_HEAD: Final[str] = "本次给出 1 个方案，处置流程如下（**本阶段不会执行**）："


# ======================================================================
#  处置建议的产出（多方案出表 / 单方案出流程 / 选中后出所选流程）
# ======================================================================
def _remediation_plans(flags: Any) -> tuple[RemediationPlan, ...]:
    """本轮要展示的候选方案：**优先**只展示用户选中的那一条，否则展示全部候选。"""
    selected = selected_from_flags(flags)
    if selected is not None:
        return (selected,)
    raw = flags.get("remediation_plans") if isinstance(flags, dict) else None
    return tuple(item for item in (raw or ()) if isinstance(item, RemediationPlan))


def _remediation_answer(body: dict[str, Any], explanation: dict[str, Any],
                        plans: tuple[RemediationPlan, ...], answer: Any,
                        _status: str = "", selected: RemediationPlan | None = None,
                        flags: Any = None) -> None:
    """把候选方案（或所选方案的流程）写成答案主体。

    ★ `report` = 一段总结 + 明细块的文本形式（非 WebUI 的调用方只读它，必须自足）；
      `explanation.answer.details/details_kind` 给前端渲染成表格 —
      这两处与既有的"模型总结"走**同一个**契约形状。
    ★ 文案里明说：**本阶段不会执行**；人工方案**需要人工在代码/迁移里操作**。
    """
    kind, rows = format_plans(plans)
    outcome = advisor_outcome(body, plans, flags)
    explanation["advisor_outcome"] = outcome
    chosen = selected
    if chosen is not None:
        head = (f"你选的方案是「{chosen.title}」（风险 {chosen.risk_level}）。"
                + ("★ 这一步**需要人工在代码或迁移里操作**，我这边没有写通道。"
                   if chosen.is_manual else "★ 本阶段仍**不会执行**，需要你确认后才可能落地。"))
    elif len(plans) == 1:
        head = _PLAN_FLOW_HEAD
    else:
        head = (f"本次给出 {len(plans)} 个候选方案（见下表 —— "
                f"回「方案 2」或动作名，我就把**你选的那条**细化成处置流程）：")
    prefix = str(body.get("report") or "").strip()
    if prefix:
        explanation["internal_report"] = prefix
    head = outcome["summary"] + (" " + head if outcome["status"] == "plans_ready" else "")
    # ★★ 顾问边界（与导出件**同一句**，免得两处口径漂移）：给方案与流程，
    #   不代执行、不改代码 —— 用户不需要从"本阶段不会执行"里自己推断这件事。
    body["report"] = f"{head}\n\n{ADVISOR_NOTE}\n\n{_details_text(rows, kind)}".strip()
    explanation["answer"] = {
        "source": "remediation_plans",
        "note": "处置建议经证据与风险校验收口（多方案出表 / 单方案出流程）；"
                "本阶段不执行任何变更",
        "summary": head + " " + ADVISOR_NOTE,
        "details": rows,
        "details_kind": kind,
        "advisor_status": outcome["status"],
    }
    explanation["remediation_plans"] = [plan.to_dict() for plan in plans]
    explanation["selected_plan_id"] = chosen.option_id if chosen is not None else ""


def _answer_body(body: dict[str, Any], explanation: dict[str, Any],
                 facts: dict[str, Any], status: str, intent: Intent,
                 answer: Optional[dict[str, Any]]) -> None:
    """答案主体 = 模型写的那段（没有就用**同样是白话**的模板）+ 机械兜底的具体标识。

    ★★ 模型把证据里的具体标识（表名 / 错误类型）概括掉时，就在这里**如实列出** ——
      不重写模型的话、也不假装是它说的：用户要靠这些目标去核对，笼统的「某张表」等于没给线索。
    """
    internal = str(body.get("report") or "")
    text = ((answer or {}).get("text") or template_answer(status, intent.value, facts))
    missing = missing_identifiers(text, facts)
    if missing:
        text = f"{text}\n涉及的具体目标：{'、'.join(missing)}（见下方证据）"
    body["report"] = text
    if internal:
        explanation["internal_report"] = internal
    explanation["answer"] = {
        "source": (answer or {}).get("source") or "template",
        "note": (answer or {}).get("note") or "",
        "identifiers_added": missing,
        # ★★ 把**结构化那份**给前端（表格/JSON 两种视图由它渲染）——
        #   放在诊断容器里，**不加顶层契约字段**。
        "summary": (answer or {}).get("summary") or "",
        "details": (answer or {}).get("details") or [],
        "details_kind": (answer or {}).get("details_kind") or "table",
        "knowledge":(answer or {}).get('knowledge'),
        "metric_result":(answer or {}).get('metric_result'),
    }
    problems = explanation.get("problems") or []
    if intent is Intent.INCIDENT_RCA and problems:
        _complete_problem_answer(body, explanation, problems)


def _complete_problem_answer(body: dict[str, Any], explanation: dict[str, Any],
                             problems: Sequence[dict[str, Any]]) -> None:
    """保留完整问题清单；模型总结或主根因排序不能删掉其他真实问题。"""
    answer = explanation["answer"]
    observed = "；".join(f"{item['service']}：{item['title']}" for item in problems)
    summary = (f"在本次查询窗口的采样中，发现 {len(problems)} 个服务自身的问题："
               f"{observed}。这份清单只列已观测故障；采样之外的情况、各问题的深层原因"
               "以及此刻是否仍在发生，需要进一步核查。")
    rows = problem_rows(problems)
    if answer.get("summary"):
        rows.append({"label": "排查分析", "value": answer["summary"]})
    rows.extend(answer.get("details") or [])
    answer.update(summary=summary, details=rows, details_kind="table")
    body["report"] = summary + "\n\n" + _details_text(rows, "table")


def _record_export_snapshot(explanation: dict[str, Any], intent: Intent, flags: Any) -> None:
    """事故保存一次生成时间与展示方案，下载直接读取，不再生成。"""
    explanation["export_generated_at"] = datetime.now(timezone.utc).isoformat()
    if intent is Intent.REMEDIATION:
        explanation["remediation_plans"] = [plan.to_dict() for plan in _remediation_plans(flags)]


def _append_export_hint(body: dict[str, Any], message: str) -> None:
    """用户说了「导出/下载/保存为 md」→ 在答案末尾指出入口（**只加话，不加契约字段**）。"""
    if not message or not _EXPORT_ASK.search(str(message)):
        return
    report = str(body.get("report") or "").strip()
    if _EXPORT_HINT in report:
        return
    body["report"] = f"{report}\n\n{_EXPORT_HINT}".strip()
    body.setdefault('explanation',{}).setdefault('answer',{})['report_note']=_EXPORT_HINT


def _details_text(rows: Sequence[Mapping[str, Any]], kind: str) -> str:
    """明细块的文本形式（复用答案模块的唯一渲染口，免得两处各拼一份）。"""
    from .answer import render_details  # noqa: PLC0415 —— 避免模块级相互引用

    return render_details(rows, kind)


# ======================================================================
#  话术（UI 文案；不进 YAML —— 它不是"证据表"，也不是"裁决逻辑"）
# ======================================================================
_OUT_OF_SCOPE_REPORT: Final[str] = (
    "这个问题超出了我的职责范围。我是 AIOps 运维助手，"
    "负责排查线上故障、查指标现状、评估变更影响与执行受控的处置动作。"
    "请换一个与系统运行相关的问题。"
)

_KNOWLEDGE_QA_REPORT: Final[str] = (
    "知识问答（RAG 检索内部规范/流程）将在 **P4** 提供；"
    "本期**显式降级为拒答**，而不是给一个没有依据的答案。"
    "如果你其实是想排查故障或查指标，请直接描述现象。"
)

#: ★ 「未识别」的话术：**不许**说成"超出职责范围" ——
#:   用户问的是运维的事，只是这句话没被听懂；真实原因（规则未定案/模型不可用）
#:   会出现在 `degraded` 里，`clarify_reason` 也带着判据。
_CLARIFY_REPORT: Final[str] = "信息不足，先确认几个关键点再继续。"

_UNRECOGNIZED_REPORT: Final[str] = (
    "这句话我没能判定出你想做什么（不是「与运维无关」—— 我看出你问的是系统的事）。"
    "请从下面的选项里选一个，或换一种说法描述你想做什么。"
)

#: 各意图"接下来会查什么"。
#:
#: ★★ 这里修的是**口径**：`INCIDENT_RCA` 的取证链路**早已落地**，
#:   所以话术里**不许**再写「接入工具后…」—— 用户读到"接入后"会以为能力还没做，
#:   而真实原因可能是"这次没查到"（`degraded` 里写明是哪一路）。
#: ★ 反过来，**当前阶段没有落地查询路径**的意图（`METRIC_QUERY` / `CHANGE_IMPACT`）
#:   必须**明说这是能力边界**，并给一条**可执行的**替代问法 ——
#:   用户最需要的是"我该怎么问才能拿到结论"，不是一句模糊的"接入后会查"。
_NEXT_STEPS: Final[dict[str, str]] = {
    Intent.INCIDENT_RCA.value: (
        "该意图**已接取证链路**：会查该服务的指标拐点（promql_read）、"
        "同窗口的错误日志（logql_read）与慢链路（traceql_read），再做归因；"
        "若本轮没有证据，`degraded` 里会写明是执行器未接入还是哪一路信号没查成。"
    ),
    Intent.METRIC_QUERY.value: (
        "该意图走**单 Agent 轻链路**：用 `metrics_agent` 查该服务的"
        "延迟 / 错误率 / 连接池 / 故障注入等指标，把**当前值**原样报给你"
        "（当前值本身就是答案，没异常也照样给数）。"
        "★ 它**不判异常、不下根因结论**（`concluded` 的语义是『已定位根因』）——"
        "要定位原因，请描述异常现象再问一次，例如「order 最近 P99 延迟是不是涨了、为什么」。"
    ),
    Intent.CHANGE_IMPACT.value: (
        "★ 该意图在当前阶段**没有落地查询路径**（只有 `INCIDENT_RCA` 的完整取证链路与"
        "`METRIC_QUERY` 的轻链路）—— 本次不会有证据与结论。"
        "想要结论，请把问题落到「出现了什么异常」上，例如"
        "「昨天的发布之后 order 开始报错，为什么」。"
    ),
    Intent.REMEDIATION.value: (
        "★ **我只做顾问**：给方案与流程、依据与风险，**不代执行、不改代码**。"
        "**处置动作本阶段不会执行**（没有任何写通道）。等写通道接入后："
        "先出预案与回滚方案 → 风险定级 → 需要审批的动作等人工批准，"
        "且必须 `options.allow_write=true` 才可能落地。"
        "★ 本阶段即使传了 `allow_write=true` 也不会有任何动作被执行"
        "（响应里的 `budget` 会如实说明这一点）。"
    ),
}

#: ★ 用户在对话里说「导出 / 下载 / 保存为 md」时，**回复里要指出入口在哪**
#: （不许静默什么都不说 —— 用户会以为这件事做不到）。
_EXPORT_HINT: Final[str] = (
    "要留档的话：点答案卡上的「保存为 md 文件」即可 —— 内容由服务端按本次排查的"
    "结论、证据与方案生成，保存位置由你在系统保存框里自选。")
_EXPORT_ASK: Final[re.Pattern[str]] = re.compile(
    r"导出|下载|保存为\s*md|存成\s*md|导出成", re.IGNORECASE)


class RenderingMixin:
    """把（状态 + 结构化事实）**渲染**成响应体（唯一渲染入口）。"""

    # ------------------------------------------------------------------
    def _render(self, state: Any, facts: dict[str, Any], request: ChatRequest, *,
                trace_id: str, usage: Sequence[Any] = (),
                degraded: Optional[list[str]] = None) -> dict[str, Any]:
        """把编排层的（状态 + 结构化事实）渲染成响应体。**唯一渲染入口。**

        骨架（字段名与顺序）来自 `empty_contract_body` 这唯一来源 —— 这里只**覆盖取值**；
        三块内容的构造在下面三个方法里。字段一个都不能少：兜底路径也走同一份骨架。
        """
        outcome = state.route
        intent = state.intent or Intent.OUT_OF_SCOPE
        status, extra = self._render_report(state, facts, intent)
        body = empty_contract_body(
            request.session_id, trace_id=trace_id,
            turn_id=ensure_turn_id(request),
            write_allowed=bool(getattr(request.options, "allow_write", False)))
        body.update({
            "incident_id": state.incident_id,
            "status": status,
            "intent": _intent_body(outcome, intent),
            "slots": state.slots.model_dump(mode="json"),
            "clarification": self._clarification_payload(state, facts, intent),
            # ★ 根因（契约形状）。`abstained`/`budget_exhausted`/`rejected` 时为 null
            "root_cause": facts.get("root_cause"),
            "hypotheses": facts["hypotheses"],
            "evidence": [item.model_dump(mode="json") for item in state.evidence],
            "rounds": facts["rounds"],
            "cost": _cost_body(facts, usage),
            "unlocked_tools": list(getattr(outcome, "unlocked_tools", ()) or ()),
            "degraded": self._degraded_notes(state, facts, outcome, degraded),
            "unresolved_entities": list(
                getattr(outcome.result, "unresolved_entities", ()) if outcome else ()),
            "budget": facts["budget"],
            "open_questions": facts["open_questions"],
            "degraded_signals": facts["degraded_signals"],
            "termination": facts["termination"],
            # ★ 写意图的**预案与风险分级**。本阶段没有执行端，
            #   所以这里只给"打算做什么、什么级别、要不要人批"，绝不假装执行。
            "proposed_action": facts.get("proposed_action"),
            "risk": facts.get("risk"),
            # ★ 没提案时**必须说明为什么**（认出了动作但没认出目标 /
            #   客户端要求 dry_run_only）—— 否则用户只看到 `proposed_action: null`。
            "action_blocked": facts.get("action_blocked"),
            "explanation": _explanation_body(facts, outcome),
        })
        body.update(extra)
        self._finish_answer(body, facts, status, intent, getattr(state, "flags", None),
                            request.message)
        from ..attribution.v2.presentation import finish
        finish(body)
        from ..webprobe.presentation import apply as show_active
        show_active(body,(getattr(state,'flags',None) or {}).get('active_probe'))
        return body

    @staticmethod
    def _finish_answer(body: dict[str, Any], facts: dict[str, Any], status: str,
                       intent: Intent, flags: Any = None, message: str = "") -> None:
        """把 `report` 收敛成**白话答案**，内部叙述挪进 `explanation.internal_report`。

        · 答案：模型写的（`facts["answer"]`），没有就用**同样是白话**的模板；
        · 内部叙述：渲染层拼的那段（含章节号/工具名/判据）→ 只在**折叠区**显示；
        · `explanation.answer` 如实标注这份答案是谁写的（可审计）。
        · ★★ 处置诉求且**有候选方案**时，答案的主体是**方案表 / 处置流程** ——
          它是代码算出来的（`remediation.plans`），不是模型编的措辞。
        · ★ 用户问「导出/下载/保存为 md」时，答案末尾要**指出入口**（不许静默）。
        """
        explanation = body.get("explanation")
        if not isinstance(explanation, dict):            # 理论上不会；坏了也不许炸
            explanation = {}
            body["explanation"] = explanation
        _record_export_snapshot(explanation, intent, flags)
        answer = facts.get("answer") if isinstance(facts.get("answer"), dict) else None
        plans = _remediation_plans(flags)
        if plans and intent not in _TAILORED_ANSWER_INTENTS:
            _remediation_answer(body, explanation, plans, answer, status,
                                selected_from_flags(flags), flags)
            _append_export_hint(body, message)
            return
        if status in _ANSWER_STATUSES and intent not in _TAILORED_ANSWER_INTENTS:
            _answer_body(body, explanation, facts, status, intent, answer)
        else:
            # 澄清 / 越界用固定话术；知识问答使用已召回来源的答案。
            explanation["answer"] = {
                "source": "fixed_wording",
                "note": "该状态用固定话术（澄清/越界），不走答案总结",
            }
        _append_export_hint(body, message)

    @staticmethod
    def _degraded_notes(state: Any, facts: dict[str, Any], outcome: Any,
                        degraded: Optional[list[str]]) -> list[str]:
        """拼出 `degraded` 列表：意图层的降级/冲突/丢弃 + 编排层的降级 + 缺失/空信号。

        ★ 三句话必须都在这里说：**"没查成"**（缺失源）与**"查了没异常"**（空信号）
          语义完全不同，混起来就是把"没看到"当成"没有"。
        """
        notes: list[str] = list(degraded or [])
        notes.extend(getattr(outcome.trace, "degraded", ()) if outcome else ())
        notes.extend(getattr(outcome.trace, "conflicts", ()) if outcome else ())
        notes.extend(getattr(outcome.trace, "dropped", ()) if outcome else ())
        notes.extend(facts["degraded_detail"])
        if facts["degraded_signals"]:
            notes.append("缺失的信号源（没查成，**不等于**没有异常）："
                         + "、".join(facts["degraded_signals"]))
        if facts["empty_signals"]:
            # ★★ 措辞按**链路**分（真环境实测）：完整取证链路的"empty"确实是
            #   "查了，这一路没看出异常"；而**轻链路（查现状）根本不判异常** ——
            #   同一句话放到它身上就是**不成立的声明**（把"这条指标没有可报的当前值"
            #   说成"没有异常"），是同一族缺陷（"没有数据" ≠ "没有异常"）。
            notes.append((("没有可报的当前值（这条指标本次没有数据 —— "
                           "**不是**「正常」，也**不是**「没查成」）：")
                          if state.intent in LIGHT_INTENTS else "查了但没异常：")
                         + "、".join(facts["empty_signals"]))
        # ★★ 答案总结**降级要响**（用户 2026-10-03 实测上报）：模型那次调用失败时，
        #   原因原来只写在 `answer.note`（藏在折叠的"推理明细"里）→ 用户看到的是一堵
        #   证据墙，却不知道为什么这一版读起来不一样。事实与结论都没变，**只是措辞**。
        answer = facts.get("answer") if isinstance(facts.get("answer"), dict) else None
        note = str((answer or {}).get("note") or "").strip()
        if note and str((answer or {}).get("source") or "") == "template":
            notes.append(f"★ 这次的答案是**兜底模板措辞**（{note}）—— "
                         f"事实与结论没有变化，只是没走成模型总结")
        return notes

    # ------------------------------------------------------------------
    def _clarification_payload(self, state: Any, facts: dict[str, Any],
                               intent: Intent) -> Optional[dict[str, Any]]:
        """拼出响应里的 `clarification`：**编排层裁决之后**的那一份（唯一出口）。

        ★ 与意图层给的原始 payload 的差别只有一个：多轮预算可能压掉了几个问题，
          压掉了什么会写在 `suppressed` 里（不能悄悄少问）。
        """
        decision = facts.get("clarify")
        if decision and decision.get("ask"):
            payload: dict[str, Any] = {
                "questions": decision["questions"],
                "reason": decision["reason"],
            }
            if decision.get("suppressed"):
                payload["suppressed"] = decision["suppressed"]
            if decision.get("requires_confirmation"):
                payload["requires_confirmation"] = True
            return payload
        return None

    # ------------------------------------------------------------------
    def _render_report(self, state: Any, facts: dict[str, Any],
                       intent: Intent) -> tuple[str, dict[str, Any]]:
        """按 `state.status` 分派到四个 `_extra_*` 构造函数，返回 `(status, 附加字段)`。

        ★ 文案留在这里（它是 UI 的事），编排层只给**结构化事实** ——
          这样"判据"与"话术"不会互相污染。
        ★ 附加字段就是随 status 变化的那几个（`report`/`next_steps`/`reason` 族）；
          固定骨架由 `empty_contract_body` 给，两者拼起来才是完整响应体。
        """
        status = state.status
        termination = facts.get("termination") or {}
        if status == "clarify":
            return "clarify", self._clarify_extra(facts, intent)
        if status == "answered":
            # ★ 契约修订 21：值问答给了数 → 附加字段与 `abstained` **同一族**
            #   （都要 `abstained_reason` 那类"为什么是现在这个样子"的说明），
            #   但**不**再声称拒答：它答了。
            return "answered", _answered_extra(facts, intent, termination)
        if status == "abstained":
            return "abstained", self._abstained_extra(facts, intent, termination)
        if status == "budget_exhausted":
            return "budget_exhausted", _budget_exhausted_extra(facts, termination)
        # ---- concluded：这条分支意味着**真的有根因** ----
        return "concluded", _concluded_extra(facts, intent, termination)

    @staticmethod
    def _clarify_extra(facts: dict[str, Any], intent: Intent) -> dict[str, Any]:
        """构造 `clarify` 的附加字段（含"未识别但相关 → 转澄清"的判据）。"""
        if intent is Intent.OUT_OF_SCOPE:
            return {
                "report": _UNRECOGNIZED_REPORT,
                "clarify_reason": (
                    "未识别但与系统相关 → 转为澄清（而不是回「超出职责范围」）："
                    + str((facts.get("clarify") or {}).get("reason") or "")[:160]),
            }
        return {
            "report": _CLARIFY_REPORT,
            "clarify_reason": str((facts.get("clarify") or {}).get("reason") or "")[:160],
            "next_steps": _NEXT_STEPS.get(intent.value, ""),
        }

    @staticmethod
    def _abstained_extra(facts: dict[str, Any], intent: Intent,
                         termination: dict[str, Any]) -> dict[str, Any]:
        """构造 `abstained` 的附加字段。

        ★ `abstained` 的含义是"**拒答（正确行为）**：展示已收集证据 +
          建议人工方向" —— 走这条路时**给不出根因**，原因写在报告里
          （原因来自仲裁，而不是"能力没做"）。
        """
        if intent is Intent.OUT_OF_SCOPE:
            return {
                "report": _OUT_OF_SCOPE_REPORT,
                "abstained_reason": (
                    "OUT_OF_SCOPE：与运维无关的输入（意图体系的兜底类）；"
                    "判据 = 没有抽到任何**用户明说**的服务/症状/时间窗/"
                    "变更单/SLO 等系统相关信号"),
            }
        if intent is Intent.KNOWLEDGE_QA:
            return {
                "report": _KNOWLEDGE_QA_REPORT,
                "abstained_reason": "KNOWLEDGE_QA 在 P1 显式降级为拒答",
                "planned_in": "P4",
            }
        root = facts.get("root_cause")
        arbitration = facts.get("arbitration") or {}
        return {
            "report": _abstained_report(facts, intent, arbitration),
            "abstained_reason": str(termination.get("reason")
                                    or "证据不足以定论（按拒答处理）"),
            "next_steps": _NEXT_STEPS.get(intent.value, ""),
            "termination_reason": str(termination.get("reason") or ""),
            "root_cause_absent_reason": (
                "仲裁未给出根因（分数未达阈值 / 候选接近 / 全部低分 / 候选被反事实检验否决）"
                if root is None else ""),
        }


def _abstained_report(facts: dict[str, Any], intent: Intent,
                      arbitration: dict[str, Any]) -> str:
    """拼出 `abstained` 的那句话（★ 三件事必须说清）。

    ① **是不是能力边界**：不在 `EVIDENCE_INTENTS` 里的意图（`CHANGE_IMPACT` 等）
       在当前阶段**压根不取证** —— 不说清的话，用户看到"没有证据"分不出
       "没这个能力"与"这次没查到"。
       ★ 走单 Agent 轻链路的 `METRIC_QUERY` **已经不在这一类**了，
       所以它的"没有根因"是**正常语义**（查现状不构成根因），不是能力边界。
    ② **本轮到底查到了什么**：证据条数 / 候选数 / 缺失信号源。
    ③ **仲裁为什么不下结论** —— 但仲裁**没给理由时不许留一句
       「仲裁给出的理由：」（读起来像被截断的半个句子）。
    """
    inquiry = intent is Intent.METRIC_QUERY
    boundary = ("" if intent in EVIDENCE_INTENTS else
                f"★ 意图「{intent.value}」在当前阶段**没有落地查询路径**"
                f"（只有 INCIDENT_RCA 的完整取证链路与 METRIC_QUERY 的轻链路）→ "
                f"本轮不会取证，因此没有证据也没有根因：**这是能力边界，不是这次排查失败**。")
    gathered = (f"本轮取证：{facts['evidence_count']} 条证据（{facts['rounds']} 轮）；"
                if facts["evidence_count"] else
                ("" if boundary or inquiry else "本轮没有取到证据；"))
    candidates = (f"未排除的候选 {len(facts['hypotheses'])} 个"
                  f"（打分口径见 explanation.scoring）；"
                  if facts["hypotheses"] else "")
    missing = (f"缺失信号源：{'、'.join(facts['degraded_signals'])}；"
               if facts["degraded_signals"] else "")
    note = (f"仲裁给出的理由：{arbitration['report']}"
            if arbitration.get("report") and not inquiry else "")
    if inquiry:
        # ★ 轻链路不判异常 → 假设候选与仲裁结论对它**没有意义**，
        #   与其贴一段"按未排除的候选逐个核对变更记录"（那是排查建议），
        #   不如直说这件事本身（否则用户会以为"是不是漏了归因"）。
        note = "（轻链路只查现状、不判异常，因此本次不产出根因候选与仲裁结论）"
    if boundary:
        head = f"已识别意图「{intent.value}」"
    elif inquiry:
        head = (f"已识别意图「{intent.value}」并按**单 Agent 轻链路**查了现状"
                f"（spec §3.1）：指标当前值就是答案本身，**不下根因结论**"
                f"（`concluded` 的语义是『已定位根因』）")
    else:
        head = f"已识别意图「{intent.value}」并完成归因"
    return (f"{head}"
            + ("" if boundary or inquiry else "，但**不下根因结论**")
            + ("。" if boundary or inquiry else
               "（`abstained` 是契约认可的『拒答』，不是失败）。")
            + f"{boundary}{gathered}{candidates}{missing}{note}"
            f"建议方向：{_NEXT_STEPS.get(intent.value, '')}")


def _answered_extra(facts: dict[str, Any], intent: Intent,
                    termination: dict[str, Any]) -> dict[str, Any]:
    """构造 `answered` 的附加字段（契约修订 21：**值问答给出了结果**）。

    ★ 与 `abstained` 的差别只有一句：这里**答了**（`report` 是那段带数值的白话答案），
      所以不再有"未得出结论"的措辞；`answered_reason` 说清"这是查询结果，不是故障结论"。
    """
    metric = (facts.get("metric_answer") or {}).get("answer") or {}
    if intent is Intent.KNOWLEDGE_QA:return {"report":"知识参考回答","answered_reason":"已检索生效知识，未进行现场故障归因"}
    value = metric.get("value")
    extra: dict[str, Any] = {
        # ★ 与 `abstained` 共用同一段**内部叙述**（含章节号/判据）—— 它只进折叠区，
        #   答案正文仍由 `_answer_body` 用"带数值的那段"覆盖。
        "report": _abstained_report(facts, intent, termination),
        "answered_reason": (
            "查数值：按用户所问给出结果（值问答不等于故障结论；"
            "要判断有没有问题请按故障排查）"),
        "answer": {"metric": metric.get("metric") or "", "value": value,
                   "unit": metric.get("unit") or "", "method": metric.get("method") or "",
                   "steps": list(metric.get("steps") or []),
                   "caveats": metric.get("caveats") or "",
                   "source": (facts.get("metric_answer") or {}).get("source") or ""},
    }
    if termination:
        extra["termination"] = termination
    return extra


def _budget_exhausted_extra(facts: dict[str, Any],
                            termination: dict[str, Any]) -> dict[str, Any]:
    """构造 `budget_exhausted` 的附加字段（★ 已查到的东西照给，但**明说是候选不是根因**）。"""
    best = (facts["hypotheses"] or [None])[0]
    return {
        "report": (f"本次排查**触到预算上限**，已按预算规则降级收口："
                   f"{termination.get('reason', '')}。"
                   f"当前证据 {facts['evidence_count']} 条"
                   f"、候选假设 {len(facts['hypotheses'])} 个"
                   + (f"，其中最佳候选 `{best.get('rank')}` "
                      f"{best.get('statement', '')[:60]}（分数 {best.get('score')}）"
                      if isinstance(best, dict) else "")
                   + " —— 它们**只是候选**，不构成根因结论。"),
        "budget_reason": str(termination.get("reason") or ""),
        "open_questions": facts["open_questions"],
    }


def _concluded_extra(facts: dict[str, Any], intent: Intent,
                     termination: dict[str, Any]) -> dict[str, Any]:
    """构造 `concluded` 的附加字段（这条分支意味着**真的有根因**）。"""
    root = facts.get("root_cause") or {}
    supporting = root.get("supporting") or []
    others = facts.get("hypotheses") or []
    return {
        "report": (
            f"**根因**：{root.get('statement', '')}"
            f"（类别 {root.get('category')}，分数 {root.get('score')}，"
            f"支持证据 {len(supporting)} 条：{', '.join(supporting[:4])}）。"
            f"本次取证 {facts['evidence_count']} 条（{facts['rounds']} 轮）；"
            f"打分口径：{(facts.get('explanation') or {}).get('scoring_kind')}。"
            + (f"★ 未被排除的其他可能："
               + "、".join(f"{item.get('hypothesis_id')}({item.get('score')})"
                           for item in others[1:3])
               + "。" if len(others) > 1 else "")
            + (f"缺失信号源：{'、'.join(facts['degraded_signals'])}"
               f"（可能影响结论强度）。" if facts["degraded_signals"] else "")
        ),
        "next_steps": _NEXT_STEPS.get(intent.value, ""),
        "termination_reason": str(termination.get("reason") or ""),
    }


def _intent_body(outcome: Any, intent: Intent) -> dict[str, Any]:
    """拼出响应里的 `intent` 段（没有路由结果时如实落兜底值，而不是编一个）。"""
    return {
        "name": outcome.result.intent if outcome else intent.value,
        "confidence": outcome.result.confidence if outcome else 0.0,
        "confidence_raw": outcome.result.confidence_raw if outcome else 0.0,
        "source": outcome.result.source.value if outcome else "fallback",
        "route": outcome.result.route if outcome else "rule",
        "explanation": outcome.trace.calibration_note if outcome else "",
    }


def _cost_body(facts: dict[str, Any], usage: Sequence[Any]) -> dict[str, Any]:
    """拼出响应里的 `cost` 段（`wall_ms` 由 `handle()` 按这一轮**真实**耗时填）。"""
    return {
        "input_tokens": sum(int(getattr(r, "input_tokens", 0) or 0) for r in usage),
        "output_tokens": sum(int(getattr(r, "output_tokens", 0) or 0) for r in usage),
        "tool_calls": facts["tool_calls"],
        "wall_ms": 0.0,
    }


def _explanation_body(facts: dict[str, Any], outcome: Any) -> dict[str, Any]:
    """拼出响应里的 `explanation` 段（规则命中 / 模型调用 / 打分口径 / 仲裁 / Critic）。"""
    # ★ 只在需要时算一次：原来对同一个 `outcome.trace.to_dict()` 调了两次（每次都要
    #   重新序列化一遍 model_calls）—— 现在算一次、用两次。
    trace = outcome.trace.to_dict() if outcome else {}
    return {
        "rules": trace.get("rule", {}),
        "intent_plugin": trace.get("plugin"),
        "model_calls": trace.get("model_calls", []),
        "clarification": outcome.trace.clarification_reason if outcome else "",
        "intent_latency_ms": outcome.result.latency_ms if outcome else 0,
        "scoring": facts["explanation"]["scoring"],
        "scoring_kind": facts["explanation"]["scoring_kind"],
        # ★ 先验分解（含被下限截断的类别）—— 别在渲染层再丢一次
        "prior": facts["explanation"].get("prior"),
        "reference": facts["explanation"]["reference"],
        "escalation": facts["explanation"]["escalation"],
        "graph": facts["explanation"]["graph"],
        "arbitration": facts.get("arbitration"),
        "critic": facts.get("critic"),
        "orchestration": facts["notes"][-6:],
        "problems": (facts.get("error_summary") or {}).get("problems") or [],
        "attribution_v2": facts['explanation'].get('attribution_v2'),
        "attribution_path": facts['explanation'].get('attribution_path','legacy'),
    }
