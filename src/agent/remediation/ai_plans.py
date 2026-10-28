"""处置方案的**生成**（模型写草稿，代码收口）—— 纯收口逻辑，模型调用在编排层。

--------------------------------------------------------------------------------
★ 为什么方案改成"模型生成 + 代码收口"
--------------------------------------------------------------------------------

处置方案的好坏取决于"**这类问题以前怎么处置的**"（程序性记忆里的 SOP、情景记忆里的
既往案例）与"**这一轮到底查到了什么**"，而不是几个通用动作的排列组合。
把这两样交给模型，方案才可能对上用户点名的那个毛病。

但"能建议哪些动作"必须收在白名单里 —— 让模型自由生成，就等于让它有机会编出一个
**做不到**的动作，甚至把一个禁止类动作说成低危。所以职责切成两半：

| 谁 | 负责 |
|---|---|
| 模型 | 方案要对上哪个机制、为什么是这个原因、具体步骤与预期（它读得到记忆） |
| 本模块 | 动作是否在白名单里（映射不上 → 归**人工**）、**风险等级**（既有风险判定）、L4 一律丢弃、引用的证据必须真的在本轮证据里、方案与症状是否**对症** |

★★ 三条不许越界：模型**不许**定风险等级、**不许**编证据引用、**不许**给出白名单外的
  动作。这三条都在代码侧收口（见 `plans_from_drafts`）——模型的输出只是"草稿"。
★ 本模块不产生任何执行痕迹：方案只是建议，写通道由既有的安全门把关。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from ..common.contracts import EntityRef
from ..common.enums import ActionClass, RiskLevel
from ..tools.risk import match_action
from .plans import (DEFAULT_PLAN_LIMIT, PLAN_STEPS, PlanKind, RemediationPlan,
                    _L4_ACTIONS, _blast_text, _make_plan, _risk_for)

__all__ = [
    "PLAN_ROLE",
    "PLAN_PROMPT_HEAD",
    "PlanDraft",
    "PlanDraftSet",
    "build_plan_prompt",
    "evidence_ids",
    "plan_is_targeted",
    "plans_from_drafts",
    "render_evidence_block",
    "symptom_families",
    "targeted_plans",
]

#: 这次模型调用的角色名（进记账与审计：`cost` 里能看出"方案花了一次模型"）
PLAN_ROLE: Final[str] = "plans"

#: 方案生成的**输出预算**（token）。★ 与答案总结同一个道理：思考型模型先花
#: reasoning tokens 再产出内容，预算给小了会 content 为空 → 整条方案退回代码候选。
PLAN_MIN_TOKENS: Final[int] = 4096

#: 进提示词的证据条数与摘要长度（**有界**：提示词不是日志）
EVIDENCE_IN_PROMPT: Final[int] = 12
EVIDENCE_SUMMARY_CHARS: Final[int] = 200

#: 引用证据的自由说法 → 真证据 id 的**规范形式**：`E-xxx` / `evidence_id=E-xxx` / `E-xxx,`
_REF_TOKEN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_:\-.]{2,80}")

PLAN_PROMPT_HEAD: Final[str] = (
    "你是 AIOps 值班处置顾问。请结合**记忆**（以前这类问题怎么处置的）与"
    "**本轮取证事实**，给出结构化处置方案。\n"
    "★ 四条硬要求：\n"
    "1. 每条方案的 mechanism 必须说清**为什么是这个原因/机制**（对上用户点名的症状或"
    "本轮证据里的错误类型），并解释它是怎么形成的；对不上症状的通用方案**不要给**；\n"
    "2. action 只写「要做什么」一句话（做得到的具体动作）；如果这件事**只能人工**"
    "在代码/配置/迁移里做，就在 action 与 steps 里如实写明只能人工做；\n"
    "3. evidence_refs 必须引用本轮证据里给出的那些 id（不许编造、不许引用别的轮次）；\n"
    "4. 只给建议：不要输出任何「已执行」「已重启」的说法，也不要说自己做了什么操作。\n"
    "★ 区分已证实的故障与形成原因：线程池拒绝只证明并发承载已满，不自动证明"
    "数据库连接等待或下游阻塞导致它占满。缺少线程栈/关联链路时，将具体机制写为"
    "待核查假设，并在 steps 给出核查与条件成立后的操作；不要把同窗口出现当作因果。\n"
    "★ 查询窗口内的错误日志也是本轮取证，末次指标为零不能否定窗口内的拒绝错误；"
    "说明历史样本与当前状态的区别。故障注入指标为零不能排除其他人工埋点。"
    "运行时/部署信息未给出时，不假定 JVM、Java、K8s 或特定监控入口，给通用核查方法。\n"
    "★ 兜底：确实给不出对症方案时，给一条如实说明「证据还不足以定处置方向」的方案。"
)


class PlanDraft(BaseModel):
    """模型给的一条方案草稿（**不是**最终方案：动作映射、风险级别、对症都由代码收口）。"""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(default="", description="这条方案的标题（一句话说清做什么）")
    action: str = Field(default="", description="要做什么（一句话，可执行的动作）")
    mechanism: str = Field(default="", description="对应哪个原因/机制，为什么是这个原因")
    steps: list[str] = Field(default_factory=list, description="可执行步骤，按顺序")
    expected: str = Field(default="", description="预期效果（做完之后应当看到什么）")
    preconditions: list[str] = Field(default_factory=list, description="前置条件")
    rollback: str = Field(default="", description="回滚方案")
    evidence_refs: list[str] = Field(default_factory=list,
                                     description="引用的本轮证据 id")


class PlanDraftSet(BaseModel):
    """一次方案生成的全部草稿（网关只接受结构化输出，所以用一个集合包起来）。"""

    model_config = ConfigDict(extra="ignore")

    plans: list[PlanDraft] = Field(default_factory=list)


# ======================================================================
#  一、提示词（记忆 + 本轮证据 + 用户诉求）
# ======================================================================
def build_plan_prompt(*, message: str, services: Sequence[str] = (),
                      evidence_block: str = "", memory_block: str = "",
                      context_block: str = "") -> str:
    """拼出方案生成那一次的提示词：**诉求 + 本轮证据 + 记忆 + 上下文分层**。

    ★★ 记忆与证据是**同一次调用**给的：方案要同时回答"这次查到了什么"与
      "以前这类问题是怎么处置的"，分成两次调用只会让第二次看不到第一次的上下文。
    ★ 空块**不占位**（不写"（无记忆）"这类噪声）：提示词里每一行都要有用。
    """
    parts = [PLAN_PROMPT_HEAD, f"## 用户这次的诉求\n{str(message or '').strip()}"]
    names = [str(name) for name in (services or ()) if str(name).strip()]
    if names:
        parts.append("## 点名的服务\n" + "、".join(dict.fromkeys(names)))
    parts.append(evidence_block or "## 本轮取证事实\n（本轮没有取到证据）")
    for block in (memory_block, context_block):
        if str(block or "").strip():
            parts.append(str(block).strip())
    return "\n\n".join(parts)


def evidence_ids(evidence: Sequence[Any] = (), *,
                 limit: int = EVIDENCE_IN_PROMPT) -> tuple[str, ...]:
    """本轮证据的 id 清单（**有界**）—— 提示词与引用校验共用这一份。"""
    out: list[str] = []
    for item in _prompt_evidence(evidence, limit):
        value = str(_field(item, "evidence_id") or "").strip()
        if value and value not in out:
            out.append(value)
    return tuple(out)


def render_evidence_block(evidence: Sequence[Any] = (), *,
                          limit: int = EVIDENCE_IN_PROMPT) -> str:
    """本轮证据的**可引用清单**（每条前面就是它的 id，方案要按这个 id 引用）。"""
    lines = ["## 本轮取证事实（引用时写 evidence_id，逐条如下）"]
    for item in _prompt_evidence(evidence, limit):
        eid = str(_field(item, "evidence_id") or "（无 id）")
        signal = str(_field(item, "signal") or "")
        category = str(_field(item, "category") or "")
        summary = str(_field(item, "summary") or "")
        sample = next((str(tag)[7:] for tag in (_field(item, "tags") or ())
                       if str(tag).startswith("sample=")), "")
        if sample:
            summary = f"样本：{sample}；{summary.split('；样本：', 1)[0]}"
        summary = summary[:EVIDENCE_SUMMARY_CHARS]
        lines.append(f"- {eid}｜{signal}｜{category}｜{summary}".rstrip("｜"))
    return "\n".join(lines) if len(lines) > 1 else ""


# ======================================================================
#  二、**对症**：方案必须与"用户点名的那个毛病"对上
# ======================================================================
#: 机制**族** —— 每一族是同一个机制的一组说法。
#: ★★ 判"对症"的粒度是**族**，不是词：用户说"线程池满了"、方案说"worker 被阻塞型
#:   工作占满"，说的是同一件事（有界并发承载被占满）。按词比会把这条对症的方案丢掉，
#:   而它恰恰是"宁可只给一条对症的 + 一条兜底"里最不该丢的那一条。
def _prompt_evidence(evidence: Sequence[Any], limit: int) -> list[Any]:
    """保留有界预算，但先给直接故障，防止指标快照占满前 12 条。"""
    def rank(item: Any) -> int:
        signal = str(_field(item, "signal") or "")
        tags = _field(item, "tags") or ()
        if signal in ("log_error_burst", "trace_breakpoint") and "propagated" not in tags:
            return 0
        if signal in ("log_error_burst", "trace_breakpoint"):
            return 2
        return 3 if "snapshot" in signal or "fault_injection" in signal else 1

    return sorted(evidence or (), key=rank)[:max(0, limit)]


_MECHANISM_FAMILIES: Final[tuple[tuple[str, ...], ...]] = (
    ("线程池", "连接池", "池", "worker", "thread", "pool", "队列", "queue",
     "拒绝", "reject", "阻塞", "block", "排队", "并发", "削峰", "限流", "背压",
     "abortpolicy", "饱和", "占满"),
    ("sql", "表", "table", "迁移", "migration", "schema", "死锁", "deadlock",
     "不存在", "索引", "慢查询", "锁等待"),
    ("变更", "回滚", "rollback", "发布", "release", "上线", "配置改动"),
    ("副本", "扩容", "缩容", "scale", "内存", "memory", "cpu", "磁盘", "disk",
     "资源", "上限", "limit", "oom"),
    ("下游", "downstream", "超时", "timeout", "503", "不可用", "unavailable",
     "熔断", "重试", "retry", "连接", "connection", "网络", "依赖"),
    ("延迟", "latency", "p99", "p95", "耗时", "变慢", "抖动", "毛刺"),
)


def symptom_families(*, message: str = "", evidence: Sequence[Any] = ()) -> frozenset[int]:
    """症状命中了哪些**机制族**（用户原话 + 本轮证据里出现的机制词）。

    ★ 空集 = **不设过滤**（不知道就别筛）：症状里一个机制词都认不出来时，
      按词比只会误杀，那时交给下游的锚定判据去管。
    """
    haystack = " ".join([str(message or "")]
                        + [_evidence_text(item) for item in (evidence or ())]).lower()
    return frozenset(index for index, family in enumerate(_MECHANISM_FAMILIES)
                     if any(word in haystack for word in family))


def plan_is_targeted(plan: RemediationPlan, families: Any) -> bool:
    """这条方案是不是**冲着症状那一族机制**去的（机制/标题/步骤/预期里出现该族说法）。"""
    wanted = set(families or ())
    if not wanted:
        return True
    text = " ".join((plan.title, plan.mechanism, plan.expected,
                     " ".join(plan.steps))).lower()
    return any(word in text
               for index in wanted for word in _MECHANISM_FAMILIES[index])


def targeted_plans(plans: Sequence[RemediationPlan], families: Any
                   ) -> tuple[tuple[RemediationPlan, ...], tuple[str, ...]]:
    """按机制族筛方案：**对症的留下；一条都不对症时只留一条兜底**。

    ★★ 为什么宁可少给：三条都对不上症状的方案，比一条不对症的更糟 ——
      用户会照着其中一条去动手（真机实测：问"orders 工作线程池已满"，
      给的是"扩容副本 / 重启实例 / 核对并回滚最近一次变更"，没有一条说到线程池）。
    ★ 丢掉哪一条**要留痕**：审核与用户都应当看得出"模型给过、但被判成与症状无关"。
    """
    items = list(plans or ())
    if not items:
        return (), ()
    kept = [plan for plan in items if plan_is_targeted(plan, families)]
    if kept or not set(families or ()):
        dropped = tuple(f"模型给的「{plan.title}」与症状的机制对不上 → 已丢弃"
                        for plan in items if plan not in kept)
        return tuple(kept), dropped
    dropped = tuple(f"模型给的「{plan.title}」与症状的机制对不上 → 已丢弃"
                    for plan in items[1:])
    return (items[0],), dropped


# ======================================================================
#  三、收口：草稿 → 能给用户看的方案
# ======================================================================
@dataclass(frozen=True)
class _DraftContext:
    """收口一条草稿时共用的事实（一次算好，各条草稿共用）。"""

    known_refs: tuple[str, ...] = ()
    families: frozenset[int] = frozenset()
    message: str = ""
    services: tuple[str, ...] = ()
    mentioned: str = ""
    evidence_source: str = ""


def plans_from_drafts(drafts: Sequence[Any], *, evidence: Sequence[Any] = (),
                      message: str = "", services: Sequence[str] = (),
                      mentioned: str = "", evidence_source: str = ""
                      ) -> tuple[tuple[RemediationPlan, ...], tuple[str, ...]]:
    """把模型草稿收成**能给用户看的方案**（白名单 / 风险 / L4 / 引用 / 对症全在代码侧）。

    Returns:
        `(方案, 留痕)` —— 留痕写进降级说明（哪条被丢、为什么），不静默。
    """
    ctx = _DraftContext(
        known_refs=evidence_ids(evidence), message=str(message or ""),
        services=tuple(str(name) for name in (services or ()) if str(name).strip()),
        mentioned=mentioned or message, evidence_source=str(evidence_source or ""),
        families=symptom_families(message=message, evidence=evidence))
    plans: list[RemediationPlan] = []
    notes: list[str] = []
    for draft in list(drafts or ())[:DEFAULT_PLAN_LIMIT]:
        plan, why = _draft_to_plan(draft, ctx)
        if why:
            notes.append(why)
        if plan is not None:
            plans.append(plan)
    kept, extra = targeted_plans(plans, ctx.families)
    notes.extend(extra)
    if not kept:
        return (), tuple(notes) or ("模型给的方案没有一条能收口成可执行建议",)
    return (tuple(_renumber(plan, index)
                  for index, plan in enumerate(kept[:DEFAULT_PLAN_LIMIT], start=1)),
            tuple(notes))


def _draft_to_plan(draft: Any, ctx: _DraftContext
                   ) -> tuple[RemediationPlan | None, str]:
    """一条草稿 → 一条方案（`None` = 丢弃，第二项如实说明为什么）。"""
    title = _clean(_field(draft, "title")) or _clean(_field(draft, "action"))
    mechanism = _clean(_field(draft, "mechanism"))
    action_text = _clean(_field(draft, "action")) or title
    if not title or not mechanism:
        return None, "模型给的方案缺标题或机制说明 → 已丢弃（说不清为什么的方案不该给）"
    action_class = match_action(action_text).action_class
    if action_class is None:
        plan = _manual_plan_from_draft(draft, ctx, title, mechanism, action_text)
        return plan, _refs_note(draft, plan.evidence_refs)
    if action_class.value in _L4_ACTIONS:
        return None, (f"模型给的动作 {action_class.value} 属 L4 禁止类 → **已丢弃**"
                      f"（禁止类一律不得执行，只记录审计）")
    plan = _agent_plan_from_draft(draft, ctx, title, mechanism, action_class)
    return plan, _refs_note(draft, plan.evidence_refs)


def _manual_plan_from_draft(draft: Any, ctx: _DraftContext, title: str,
                            mechanism: str, action_text: str) -> RemediationPlan:
    """白名单外的动作 → **人工方案**（如实说明"这类动作只能人工做"）。"""
    reason = (f"动作「{_short(action_text)}」不在 Agent 的可执行白名单里 → "
              f"**这类动作只能人工做**（Agent 没有写通道）")
    return _make_plan(
        option_id="", kind=PlanKind.MANUAL.value, title=title, action_class="",
        rationale=_rationale(ctx, extra=reason), expected=_clean(_field(draft, "expected")),
        risk_level=RiskLevel.L2.value, requires_approval=False,
        blast_radius=_blast_text(None, _target(ctx)),
        action=_short(action_text), mechanism=mechanism,
        evidence_refs=_refs(ctx, draft), rollback=_rollback(draft),
        steps=_steps(draft, action_text), preconditions=_preconditions(draft, ctx))


def _agent_plan_from_draft(draft: Any, ctx: _DraftContext, title: str, mechanism: str,
                           action_class: ActionClass) -> RemediationPlan:
    """白名单内的动作 → `agent_action`（**风险等级由既有风险判定给**，不采信模型）。"""
    level, approval, why = _risk_for(action_class, mentioned=ctx.mentioned,
                                    target=EntityRef.make("service", _target(ctx), None))
    return _make_plan(
        option_id="", kind=PlanKind.AGENT_ACTION.value, title=title,
        action_class=action_class.value, rationale=_rationale(ctx),
        expected=_clean(_field(draft, "expected")),
        risk_level=level, requires_approval=approval,
        blast_radius=_blast_text(None, _target(ctx)),
        action=_short(_field(draft, "action")) or title, mechanism=mechanism,
        evidence_refs=_refs(ctx, draft), rollback=_rollback(draft),
        steps=_steps(draft, title) or PLAN_STEPS.get(action_class.value, ()),
        preconditions=_preconditions(draft, ctx) or (why,))


def _rationale(ctx: _DraftContext, extra: str = "") -> str:
    """依据：**指向本轮证据的 id**（模型引用了什么，用户就能核对什么）。"""
    refs = "、".join(ctx.known_refs) or "（本轮证据 id 未取到）"
    where = f"本轮证据 {refs}"
    if ctx.evidence_source:
        where = f"{where}（出处：{ctx.evidence_source}）"
    return "依据：" + "；".join(part for part in (where, extra) if part)


def _refs(ctx: _DraftContext, draft: Any) -> tuple[str, ...]:
    """这条方案引用的证据 id：**只认本轮真的有的**（模型编的引用一律不要）。

    ★ 模型一条都没引用（或引用的都是编的）时，用**本轮真证据**补上并在留痕里说明 ——
      方案表里"引用了什么"这一栏不能是空的（那等于让用户无从核对），
      但"补齐"这件事必须说出来，不能伪装成模型引用的。
    """
    raw = _field(draft, "evidence_refs") or ()
    if isinstance(raw, (str, bytes)):
        raw = [raw]
    found: list[str] = []
    for item in raw:
        for token in _REF_TOKEN.findall(str(item or "")):
            if token in ctx.known_refs and token not in found:
                found.append(token)
    if found:
        return tuple(found[:DEFAULT_PLAN_LIMIT + 2])
    return ctx.known_refs


def _refs_note(draft: Any, refs: Sequence[str]) -> str:
    """引用被代码补齐时留痕（模型引用的证据引用不了 → 补本轮证据）。"""
    raw = _field(draft, "evidence_refs") or ()
    return ("" if raw else
            f"模型没给出可核对的证据引用 → 按本轮证据补上（{'、'.join(refs)}）")


def _steps(draft: Any, fallback: str) -> tuple[str, ...]:
    """步骤：模型给的按原样（**有界**）；一条都没有时用动作那句话兜底。"""
    raw = _field(draft, "steps") or ()
    if isinstance(raw, (str, bytes)):
        raw = [raw]
    steps = tuple(_short(item) for item in raw if _clean(item))
    return steps or ((fallback,) if fallback else ())


def _preconditions(draft: Any, ctx: _DraftContext) -> tuple[str, ...]:
    """前置条件：模型给的 + 「本阶段不执行」这条**永远成立**的事实。"""
    raw = _field(draft, "preconditions") or ()
    if isinstance(raw, (str, bytes)):
        raw = [raw]
    items = [_short(item) for item in raw if _clean(item)]
    items.append("本阶段只给建议、不执行任何变更（写通道未接入）")
    return tuple(items)


def _rollback(draft: Any) -> str:
    """回滚方案：模型没给就**如实**写"未提供"（不编一句通用的顶上）。"""
    text = _clean(_field(draft, "rollback"))
    return text if text else "回滚方案：模型未给出 → 执行前必须先补齐回滚方案（人工确认）"


def _target(ctx: _DraftContext) -> str:
    """动作目标：用户点名的服务排最前（认不出就是中性说法）。"""
    return ctx.services[0] if ctx.services else "该服务"


def _renumber(plan: RemediationPlan, index: int) -> RemediationPlan:
    """编上表格里的序号（用户会说"方案 2"）。"""
    return RemediationPlan(
        option_id=str(index), kind=plan.kind, title=plan.title,
        action_class=plan.action_class, rationale=plan.rationale,
        action=plan.action, mechanism=plan.mechanism, evidence_refs=plan.evidence_refs,
        expected=plan.expected, risk_level=plan.risk_level,
        requires_approval=plan.requires_approval, blast_radius=plan.blast_radius,
        preconditions=plan.preconditions, rollback=plan.rollback, steps=plan.steps)


# ----------------------------------------------------------------------
def _field(source: Any, name: str) -> Any:
    """双通道读一个字段：**先属性、后下标**（草稿对象与 dict 两种形状都吃）。"""
    value = getattr(source, name, None)
    if value is not None:
        return value
    if isinstance(source, Mapping):
        return source.get(name)
    return None


def _clean(value: Any) -> str:
    """一句话里的空白压平（提示词/方案表里不留换行噪声）。"""
    return " ".join(str(value or "").split())


def _short(value: Any, limit: int = 160) -> str:
    """有界的一句话（步骤/说明别把方案表撑爆）。"""
    text = _clean(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _evidence_text(item: Any) -> str:
    """一条证据的**判机制用**文本（信号 / 类别 / 摘要 / 错误类型一起看）。"""
    return " ".join(str(_field(item, name) or "") for name in
                    ("signal", "category", "summary", "error_type", "raw_ref"))
