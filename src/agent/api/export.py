"""把一次排查的**事实**渲染成可导出的 Markdown（纯函数、零 IO、零执行）。

--------------------------------------------------------------------------------
★ 这是"导出"，不是"写能力"
--------------------------------------------------------------------------------

它只做一件事：把**已经存在**的事实（问题 / 结论与不确定性 / 证据 / 候选方案）转成文本。
不碰被观测系统、不产生待确认动作、不读写任何共享状态 —— 所以它**不需要**
`options.allow_write`，也不受它影响（写通道的四种确认结局一字未动）。

--------------------------------------------------------------------------------
★ 为什么必须是纯函数
--------------------------------------------------------------------------------

"同一次排查导两次，内容不一样"是不可接受的：导出件是要贴进工单、发给人看的东西。
于是本模块**没有 `now()`、没有随机数**：生成时间由调用方以 `generated_at` 注入，
同输入两次逐字相同（可哈希、可 diff）。

--------------------------------------------------------------------------------
★ 五节固定顺序（顺序本身也是契约）
--------------------------------------------------------------------------------

`## 问题` → `## 结论与不确定性` → `## 证据` → `## 处置方案` → `## 安全声明`。
每一节都只从 `body` 里**已有的字段**取值，取不到就如实写"（未记录）" ——
不补一句看起来合理的话（那会让导出件比事实"更好看"）。
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Final, Mapping, Sequence

from ..common.enums import ActionClass, ChatStatus, DEFAULT_ACTION_LEVEL, RiskLevel
from ..remediation.plans import candidate_plans

__all__ = ["ADVISOR_NOTE", "SAFETY_NOTE", "render_markdown", "select_plans"]

#: 五节的标题与顺序（**唯一**来源：正文与自检都读它）
SECTIONS: Final[tuple[str, ...]] = (
    "## 问题", "## 结论与不确定性", "## 证据", "## 处置方案", "## 安全声明")

#: 用户提供的信息里可能夹着**本系统不该建议的动作**（例如在聊天里讨论过删除）。
#: 白名单内（且非 L4）的动作名原样保留，**其余一律抹掉** —— 只认白名单内的是
#: 因为"能出现在导出件里的动作"必须就是"代码敢生成预案的那些动作"。
_SAFE_ACTIONS: Final[frozenset[str]] = frozenset(
    str(member.value) for member in ActionClass
    if str(DEFAULT_ACTION_LEVEL.get(str(member.value), "")) != RiskLevel.L4.value)
_WORD_CHARS: Final[str] = "A-Za-z0-9_"
_ACTION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![" + _WORD_CHARS + r"])(" + "|".join(
        re.escape(str(member.value)) for member in ActionClass)
    + r")(?![" + _WORD_CHARS + r"])")

#: 契约里的状态值 → 大白话（导出件是给人看的，不印枚举名；键取自状态枚举）
_STATUS_PLAIN: Final[dict[str, str]] = {
    ChatStatus.CONCLUDED.value: "已定位根因",
    ChatStatus.ABSTAINED.value: "未得出结论",
    ChatStatus.BUDGET_EXHAUSTED.value: "预算用尽后停止",
    ChatStatus.CLARIFY.value: "还在等你补充信息",
    ChatStatus.REJECTED.value: "已按安全门拒绝",
    ChatStatus.FAILED.value: "本次未跑完（系统错误）",
}

#: 安全声明的前半句 —— 措辞由本模块**一处**给出（端点与前端都不再各写一份）
SAFETY_NOTE: Final[str] = (
    "本阶段不执行任何变更；要执行需人工审批 + `options.allow_write=true`。")
#: 顾问边界（答案侧与导出件**同一句**，免得两处口径漂移）
ADVISOR_NOTE: Final[str] = (
    "我只做顾问：给方案与流程，不代执行、不改代码。")


def render_markdown(*, session_id: str, incident_id: str, body: Mapping[str, Any],
                    plans: Sequence[Any] = (), generated_at: str = "") -> str:
    """把一次排查的事实渲染成 Markdown（时间由参数注入，**纯函数**）。

    Args:
        session_id: 会话标识（与问诊请求同源，只用于可追溯）。
        incident_id: 本次排查的编号；写进一级标题 —— 导出件脱离对话也能对上号。
        body: 响应体（或 `state` 快照）；只读其中的证据 / 假设 / 结论 / 降级说明。
        plans: 候选方案（`RemediationPlan` 或同形映射）；为空则如实写"本次没有候选方案"。
        generated_at: 生成时间（ISO 串）。**由调用方注入** —— 纯函数不许自己看表。
    """
    plans = tuple(plans or ())
    body = body if isinstance(body, Mapping) else {}
    from ..presentation.projection import saved
    if saved(body):
        return _saved_markdown(session_id,incident_id,body,plans,generated_at)
    from ..attribution.v2.presentation import markdown
    parts=[
        _title(session_id, incident_id, generated_at),
        _section(SECTIONS[0], _problem_lines(body)),
        _section(SECTIONS[1], _conclusion_lines(body)),
        _section(SECTIONS[2], _evidence_lines(body)),
        _section(SECTIONS[3], _plan_lines(plans)),
        _section(SECTIONS[4], _safety_lines(plans)),
        markdown(body),
        _dialogue_lines(body),
    ]
    audit=_attempt_lines(body)
    if audit:parts.append(audit)
    return "\n".join(parts)


def _saved_markdown(session_id,incident_id,body,plans,generated_at):
    from ..presentation.markdown import problem_lines,conclusion_lines,reference_lines
    safety=_safety_lines(plans)
    if (body.get('explanation') or {}).get('active_probe'):
        safety=['修复建议本阶段仍由顾问提供，不自动执行修复变更。主动HTTP核验是单独能力，只访问当前环境已登记且开放的接口；业务写请求须用户确认完整方案，超时或中断不自动重发。']
    parts=[_title(session_id,incident_id,generated_at),
        _section(SECTIONS[0],problem_lines(body)),
        _section(SECTIONS[1],conclusion_lines(body)),
        _section(SECTIONS[2],reference_lines(body)),
        _section(SECTIONS[3],_plan_lines(plans)),_section(SECTIONS[4],safety)]
    audit=_attempt_lines(body)
    if audit:parts.append(audit)
    return '\n'.join(parts)


def _attempt_lines(body):
    ex=body.get('explanation') or {};rows=ex.get('tool_attempts') or []
    if not rows:return ''
    scope=ex.get('observed_system') or {};oid=scope.get('observed_id','')
    lines=['\n## 工具调用档案\n','系统：'+scope.get('observed_name',oid)+' · '+oid]
    for row in rows:
        aid=row.get('attempt_id','');ref=f'/v1/observed-systems/{oid}/access/{aid}'
        lines.append(f"- {row.get('tool','调用终态')} · {row.get('error','')} · {row.get('persist_state','')} · [{aid}]({ref})")
    return '\n'.join(lines)+'\n'


def _dialogue_lines(body):
    ex=body.get('explanation') or {};d=ex.get('dialogue') or {}
    if not d or d.get('action')=='new_task':return ''
    a=ex.get('answer') or {}
    rows=['\n## 本轮任务推进\n',a.get('summary',''),
          '任务：'+d.get('task_id','')+'；依据版本：'+d.get('based_on_revision','')]
    rows.extend('\n### '+r['label']+'\n\n'+str(r['value']) for r in a.get('details',[]))
    for item in d.get('submissions',[]):
        rows.append('\n用户资料（'+item['verification']+'）：\n'+str(item['content']))
    return '\n'.join(rows)+'\n'


def select_plans(body: Mapping[str, Any], option_id: str = "",
                 deployment_form: str = "") -> tuple[Any, ...] | None:
    """优先读取页面已展示的方案快照；旧事故没有快照时兼容模板生成。

    ★ 快照在事故里持久保存，导出不得重新生成一份与页面不同的方案。
      显式空清单表示确实没有方案；只有旧记录缺少该字段时才重算模板。
    ★ `deployment_form`：★★ 导出件也是**用户可见文本**，机制名词必须与页面同源
      （页面说"实例"、导出件说"Pod"就是同一个事实两份说法）—— 所以部署形态这个
      事实要一路传进来，而不是在这里默认成"未配置"。
    ★ 返回 `None` 表示"`option_id` 不在候选里"（调用方据此回 422）；
      返回空元组表示"这次排查确实没有候选方案"（如实导出，不是错误）。
    """
    explanation = body.get("explanation") or {}
    if "remediation_plans" in explanation:
        plans = tuple(explanation["remediation_plans"] or ())
    elif (explanation.get('answer') or {}).get('view_version')==1:
        plans=()
    else:
        plans = candidate_plans(message=_export_message(body),
                                hypotheses=body.get("hypotheses") or (),
                                evidence=body.get("evidence") or (),
                                services=_export_services(body),
                                deployment_form=str(deployment_form or ""))
    chosen = str(option_id or "").strip()
    if not chosen:
        return plans
    for plan in plans:
        if str(_get(plan, "option_id")) == chosen:
            return (plan,)
    return None


def _title(session_id: str, incident_id: str, generated_at: str) -> str:
    """一级标题：**必须**同时含 `incident_id` 与生成时间（可追溯）。"""
    incident = str(incident_id or "").strip() or "（未记录）"
    return (f"# AIOps 处置方案导出 · {incident}\n\n"
            f"- 会话：`{str(session_id or '').strip() or '（未记录）'}`\n"
            f"- 排查编号：`{incident}`\n"
            f"- 生成时间：{str(generated_at or '').strip() or '（未记录）'}\n")


def _section(title: str, lines: Sequence[str]) -> str:
    return f"\n{title}\n\n" + "\n".join(lines or ("（本节没有可写的事实）",)) + "\n"


def _get(source: Any, key: str, default: Any = "") -> Any:
    """双通道读一个字段（契约对象与 dict 两种形状都吃）。"""
    if source is None:
        return default
    try:
        value = getattr(source, key)
    except (AttributeError, TypeError):
        value = None
    if value is not None:
        return value
    try:
        return source[key] if key in source else default
    except (TypeError, KeyError, IndexError):
        return default


def _text(source: Any, key: str) -> str:
    return str(_get(source, key, "") or "").strip()


def _joined(items: Any) -> str:
    """把列表 / 元组 / 集合 / 单值统一成顿号串（导出件里不出现 `['a', 'b']`）。"""
    if not items:
        return ""
    if isinstance(items, (str, bytes)):
        return str(items)
    if isinstance(items, (list, tuple, set, frozenset)):
        parts = [str(item).strip() for item in items if str(item).strip()]
        return "、".join(parts)
    return str(items)


def _mask_action_words(text: str) -> str:
    """把**白名单之外**的动作名抹掉（`<已屏蔽的动作名>`），白名单内的原样保留。

    ★ 为什么需要：导出件里的自由文本（用户原话 / 工具摘要 / 模型措辞）可能提到
      一个本系统根本不做的动作（如 `delete_pvc`）—— 印进导出件就等于**建议**了它。
      抹掉是**可见的**（`<已屏蔽的动作名>`），不是静默删除。
    ★ 顺带把正文里的换行折成空格：Markdown 的 `- x：` 列表项遇到内嵌换行会断成两行，
      导出件读起来像两句两句的事实（起因是入口会把"当前任务"台账并进消息）。
    """

    def _keep(match: re.Match[str]) -> str:
        return match.group(0) if match.group(0) in _SAFE_ACTIONS else "<已屏蔽的动作名>"

    flat = " ".join(str(text or "").split())
    return _ACTION_RE.sub(_keep, flat)


def _slot_labels(slots: Any) -> str:
    """只挑**定性**的那几个槽位（服务 / 环境 / 集群 / 时间窗）—— 不把整个槽位对象摊开。"""
    if not slots:
        return ""
    service = _text(_get(slots, "service", None), "name") or _text(
        _get(slots, "service", None), "id")
    parts = [f"服务={service}" if service else ""]
    for key, caption in (("env", "环境"), ("cluster", "集群"), ("time_window", "时间窗")):
        value = _text(slots, key)
        if value:
            parts.append(f"{caption}={value}")
    return "；".join(part for part in parts if part)


def _problem_lines(body: Mapping[str, Any]) -> list[str]:
    """问题：用户原话 + 槽位 + 意图与状态（把"这次在查什么"定住）。"""
    lines: list[str] = []
    original = _text(body, "user_message") or _text(body, "message")
    if original:
        # ★★ 织过上下文的那份输入**不能当成"用户原话"**：它后面挂着"（当前任务：…）（时间窗 …）"，
        #   当成原话会让人以为用户真这么说过（可追溯性被污染）。
        label = "输入（**按上下文编织后**）" if _looks_weaved(original) else "用户原话"
        lines.append(f"- {label}：{_mask_action_words(original)}")
    else:
        # ★ 事故快照里没有原话时**如实说**，不要静默少一行（读者会以为这次没有输入）。
        lines.append("- 用户原话：（事故快照未记录，"
                     "可在事故详情里按 `incident_id` 回溯）")
    labels = _slot_labels(_get(body, "slots", None))
    if labels:
        lines.append(f"- 这次排查的槽位：{_mask_action_words(labels)}")
    intent = _get(body, "intent", None)
    name = _text(intent, "name") if not isinstance(intent, str) else intent
    if name:
        lines.append(f"- 意图：`{name}`")
    status = _text(body, "status")
    advisor = (_get(body, "explanation", None) or {}).get("advisor_outcome") or {}
    if status:
        lines.append(f"- 本次状态：{advisor.get('label') or _STATUS_PLAIN.get(status, status)}（`{status}`）")
    summary_label, summary_text = _problem_summary(body)
    lines.append(f"- {summary_label}：{_mask_action_words(summary_text)}")
    return lines


#: 入口编织留下的标记（出现这些就说明这段文本**不是**用户原话）
_WEAVE_MARKERS: Final[tuple[str, ...]] = ("（当前任务：", "（时间窗 ", "（已知：", "（在等：")


def _looks_weaved(text: str) -> bool:
    """这段输入是不是被"上下文编织"追加过内容（用于如实标注，不冒充原话）。"""
    return any(marker in text for marker in _WEAVE_MARKERS)


def _problem_summary(body: Mapping[str, Any]) -> tuple[str, str]:
    """问题小结 → `(标签, 文本)`。

    ★★ 标签必须**如实**：有根因才写"定位到的根因"；只有候选假设时写"最像的解释（未确认）"；
      否则退回报告首句。把假设写成"问题小结"会让人以为那就是问题本身。
    """
    advisor = (_get(body, "explanation", None) or {}).get("advisor_outcome") or {}
    if advisor.get("summary"):
        return "已观测故障与核查边界", advisor["summary"]
    root = _root_cause_of(body)
    if root:
        return "定位到的根因", root
    items = _get(body, "hypotheses", None) or ()
    if items:
        first = _text(items[0], "statement")
        if first:
            return "最像的解释（未确认）", first
    report = str(_get(body, "report", "") or "").strip()
    return "问题", (report.splitlines()[0] if report else "（未记录）")


def _root_cause_of(body: Mapping[str, Any]) -> str:
    """根因陈述：响应体在顶层，`state` 快照在 `arbitration.root_cause` 下（两种形状都吃）。"""
    root = _get(body, "root_cause", None) or _get(
        _get(body, "arbitration", None), "root_cause", None)
    return _mask_action_words(_text(root, "statement"))


def _conclusion_lines(body: Mapping[str, Any]) -> list[str]:
    """结论与不确定性：根因（或有）→ 候选假设 → 没查成的信号 → 证据缺口。"""
    lines: list[str] = []
    root = _root_cause_of(body)
    advisor = (_get(body, "explanation", None) or {}).get("advisor_outcome") or {}
    if advisor.get("status") == "plans_ready":
        lines.append("- 顾问结果：已提供修复方案；" + str(advisor.get("summary") or ""))
    if advisor:
        lines.append("- 故障形成原因：" + (root or "尚未确认"))
    else:
        lines.append("- 结论：" + (root or "本次没有定位到根因（下列候选都没到确认线）"))
    for index, item in enumerate(_get(body, "hypotheses", None) or (), start=1):
        statement = _mask_action_words(_text(item, "statement") or "（无陈述）")
        score = _text(item, "score") or "-"
        status = _text(item, "status") or "-"
        lines.append(f"- 候选 {index}：{statement}（把握 {score}，状态 {status}）")
    degraded = _joined(_get(body, "degraded_signals", None) or ())
    lines.append("- 没查成的信号源：" + (f"{degraded}（没查成 ≠ 没有异常）"
                                   if degraded else "（无）"))
    empty = _joined((_get(body, "empty_signals", None) or ())[:6])
    lines.append("- 查了但没有异常：" + (empty if empty else "（无）"))
    gaps = _joined((_get(body, "open_questions", None) or ())[:3])
    lines.append("- 还不确定的地方：" + (_mask_action_words(gaps) if gaps
                                   else "证据只支持到上面的程度，更强的结论需要更多证据"))
    notes = _joined((_get(body, "degraded", None) or ())[:4])
    if notes:
        lines.append(f"- 本次降级说明：{_mask_action_words(notes)}")
    return lines


def _plain_label(text: str, label: str) -> str:
    """方案里的字段自带 `依据：` / `回滚方案：` 前缀时，避免导出件里出现两次标签。"""
    body = str(text or "").strip()
    return body[len(label):].strip() if body.startswith(label) else body


def _evidence_lines(body: Mapping[str, Any]) -> list[str]:
    """证据：**信号名 + 证据 id + 人话摘要**（缺 id 就如实写「（无 id）」）。"""
    items = _get(body, "evidence", None) or ()
    if not items:
        return ["（本次没有留下证据 —— 这是「没查到」，不是「没有异常」）"]
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        signal = _text(item, "signal") or "（无信号名）"
        evidence_id = _text(item, "evidence_id") or "（无 id）"
        summary = _mask_action_words(_text(item, "summary") or "（无摘要）")
        lines.append(f"{index}. `{signal}` · 证据 id `{evidence_id}`：{summary}")
    return lines


def _action_of(plan: Any) -> str:
    """方案的动作名；白名单外的取值（含 L4）一律退成占位符，不印出来。"""
    name = _text(plan, "action_class")
    return name if name in _SAFE_ACTIONS else "（无动作名：本方案不属于白名单动作）"


def _field(plan: Any, key: str, label: str = "") -> str:
    """读一条方案的字段并抹掉白名单外的动作名（`label` 非空时先去掉自带前缀）。"""
    value = _text(plan, key) or "（未记录）"
    if label:
        value = _plain_label(value, label)
    return _mask_action_words(value)


def _plan_lines(plans: Sequence[Any]) -> list[str]:
    """处置方案：每条七件事实（依据 / 预期 / 风险 / 影响面 / 前置 / 回滚 / 步骤）。"""
    if not plans:
        return ["（本次没有候选方案）"]
    lines: list[str] = []
    for plan in plans:
        lines.extend(_one_plan(plan))
    return lines


def _one_plan(plan: Any) -> list[str]:
    """一条方案的全部字段（七件事实 + 步骤流程），顺序固定。"""
    option_id = _text(plan, "option_id") or "-"
    title = _mask_action_words(_text(plan, "title") or "（无标题）")
    kind = _text(plan, "kind") or "-"
    head = f"- 类别：`{kind}`"
    if kind != "manual":
        head += f" · 动作：`{_action_of(plan)}`"
    approval = "（需人工审批）" if _get(plan, "requires_approval", False) else ""
    steps = _mask_action_words(_joined(_get(plan, "steps", None) or ())) or "（未记录）"
    lines = [f"### 方案 {option_id} · {title}", "", head]
    if kind == "manual":
        lines.append("- ★ 本方案**需要人工操作**（Agent 没有写通道，不代执行、不改代码）")
    lines += [
        f"- 建议操作：{_field(plan, 'action')}",
        f"- 对应机制：{_field(plan, 'mechanism')}",
        "- 引用证据：" + (_joined(_get(plan, "evidence_refs", None) or ()) or "（未记录）"),
        f"- 依据：{_field(plan, 'rationale', '依据：')}",
        f"- 预期效果：{_field(plan, 'expected')}",
        f"- 风险等级：{_text(plan, 'risk_level') or '-'}{approval}",
        f"- 影响面：{_field(plan, 'blast_radius')}",
        "- 前置条件：" + (_joined(_get(plan, "preconditions", None) or ()) or "（未记录）"),
        f"- 回滚方案：{_field(plan, 'rollback', '回滚方案：')}",
        f"- 步骤流程：{steps}",
        "",
    ]
    return lines


def _safety_lines(plans: Sequence[Any]) -> list[str]:
    """安全声明：顾问边界 + 不执行 + 审批与 `allow_write` + manual 类需人工。"""
    lines = [
        f"- **顾问边界**：{ADVISOR_NOTE}",
        f"- **不执行**：{SAFETY_NOTE}",
        "- 导出件是**事实的文本化**：导出动作本身不产生待确认动作、不改动被观测系统，"
        "也不受 `options.allow_write` 影响。",
        "- `manual` 类方案**需要人工操作**：改代码 / 跑迁移 / 改配置都由人来完成，我只给流程。"]

    manual = [str(_text(plan, "option_id") or "-") for plan in plans
              if _text(plan, "kind") == "manual"]
    if manual:
        lines.append(f"- 本次候选里的**人工方案**：方案 {'、'.join(manual)} —— 需要人工操作。")
    else:
        lines.append("- 本次候选里没有人工方案（仍不代执行：任何变更都要人工审批）。")
    return lines


def _utc_strftime(pattern: str, moment: Any = None) -> str:
    """时间格式化的**唯一**入口（两个公开helper都走它，免得一处改了另一处没改）。"""
    stamp = moment if isinstance(moment, datetime) else datetime.now(timezone.utc)
    return stamp.astimezone(timezone.utc).strftime(pattern)


def utc_stamp(moment: Any = None) -> str:
    """`yyyymmdd-HHMMSS`（下载文件名用）。"""
    return _utc_strftime("%Y%m%d-%H%M%S", moment)


def iso_now(moment: Any = None) -> str:
    """`yyyy-mm-ddTHH:MM:SSZ`（正文里的生成时间）。"""
    return _utc_strftime("%Y-%m-%dT%H:%M:%SZ", moment)


#: 落库原话取不到时的兜底问法（只用来决定"候选方案按什么口径生成"）
_FALLBACK_MESSAGE: Final[str] = "给出解决方案"


def _export_message(body: Mapping[str, Any]) -> str:
    """导出用的原话：落库的 `user_message`，取不到就退回**中性的**处置诉求。

    ★ 不编一句更"像样"的话：落库原话缺了就是缺了（`## 问题` 小节的原话行只认落库
      字段），这里的取值只影响"候选方案按什么口径算"，不会冒充用户说的。
    """
    for key in ("user_message", "message"):
        text = str(body.get(key) or "").strip()
        if text:
            return text
    return _FALLBACK_MESSAGE


def _export_services(body: Mapping[str, Any]) -> tuple[str, ...]:
    """从落库槽位取**目标服务**：先给服务名，再把实体 id 作为**兜底**候选。

    ★ 顺序有讲究：`candidate_plans` 只在"这句话里点到了服务名"或"清单里恰好一个"时
      才把方案锚定到它 —— 把 `svc:inventory` 这类 id 排在前面会命中不了原话。
    """
    slots = body.get("slots")
    if not isinstance(slots, Mapping):
        return ()
    service = slots.get("service")
    name = entity_id = ""
    if isinstance(service, Mapping):
        name = str(service.get("name") or "").strip()
        entity_id = str(service.get("id") or "").strip()
    elif service:
        name = str(service).strip()
    listed = slots.get("service_names")
    names = [name] if name else []
    if isinstance(listed, (list, tuple)):
        names.extend(str(item).strip() for item in listed if str(item).strip())
    if not names and entity_id:
        names.append(entity_id)
    return tuple(dict.fromkeys(item for item in names if item))
