"""反问仲裁器 `ClarifyHub`：**要不要问、问哪个、问了怎么回填**。

★ 为什么要有仲裁器（而不是让插件自己决定问不问）：
  · "该不该问"是**全局**判断（代价比较 / 预算 / 是否已经问过 / 用户是否说别问），
    每个插件各判一次必然出现"一次问三个"；
  · 插件只负责"这个场景怎么问、答案怎么用"。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional, Sequence

from ..common.enums import ErrorCode
from ..intent.classifier import ClarificationDecision, ClarificationQuestion
from .contract import (
    CLARIFY_BUDGET,
    AskContext,
    ClarificationRequest,
    SlotPatch,
)
from .registry import ClarifyRegistry

__all__ = ["ClarifyHub", "CLARIFY_BUDGET"]

#: 出题失败对用户意味着"这句我本该问、但没问" —— 必须能看见（`degraded`）也必须能排障（日志）
logger = logging.getLogger("agent.clarify.arbiter")

#: 哪些意图"环境没指定就**必须**问清"（不明确不干活）。
#: ★ 只在**没有上下文 env** 时生效；`REMEDIATION` 也在内 —— 改错环境的代价最大。
ENV_SENSITIVE_INTENTS: tuple[str, ...] = (
    "INCIDENT_RCA", "METRIC_QUERY", "CHANGE_IMPACT", "REMEDIATION")

#: baseline 说"不用问"时，**允许被升级成反问**的缺口（只放 env，理由见 `_promote`）
PROMOTE_GAPS: tuple[str, ...] = ("env",)

#: 越界不补服务；知识由qa_agent处理，不升级为运维服务/环境补槽。
NO_PROMOTE_INTENTS: tuple[str, ...] = ("OUT_OF_SCOPE", "KNOWLEDGE_QA")

# ----------------------------------------------------------------------
#  ★★ 「泛化健康询问」的识别（缺目标时要**启发式反问**）
# ----------------------------------------------------------------------
#: 健康/异常类的词 —— 用户在问"有没有事"
_HEALTH_WORDS: tuple[str, ...] = (
    "问题", "异常", "故障", "报错", "出错", "挂了", "正常", "可用", "有事",
    "怎么样", "怎么了", "有没有事", "状态")
#: 时间指代（"最近有没有问题"里的"最近"本身不足以判定，见下面的组合规则）
_TIME_WORDS: tuple[str, ...] = ("最近", "现在", "刚才", "今天", "这两天", "这段时间")
#: 系统指代
_SYSTEM_WORDS: tuple[str, ...] = ("系统", "线上", "服务", "集群", "后端", "环境", "平台")
#: ★ 明显跨域的词：命中就**不**当作健康询问（防把"今天天气怎么样"问成"你要查哪个服务"）
_OFF_DOMAIN_WORDS: tuple[str, ...] = (
    "天气", "笑话", "诗", "股票", "菜谱", "翻译", "数学", "作文", "游戏",
    "电影", "歌词", "汇率", "彩票", "星座", "旅游", "减肥")


def looks_like_health_scan(message: str) -> bool:
    """这句话是不是"**泛化的健康询问**"（在问有没有问题，却没说是谁）。

    ★ 判据（刻意写窄，宁可漏也不能把跨域请求拖进追问）：
      **健康词** 且（**时间词** 或 **系统词**），且**不含明显跨域词**。
    ★ 为什么不能只看"健康词"：`你觉得这个方案怎么样` 也含"怎么样"；
      为什么不能只看"时间词+健康词"：`今天天气怎么样` 正是这个形状 —— 靠跨域词排除。
    """
    text = str(message or "").strip()
    if not text or any(word in text for word in _OFF_DOMAIN_WORDS):
        return False
    health = any(word in text for word in _HEALTH_WORDS)
    context = any(word in text for word in _TIME_WORDS + _SYSTEM_WORDS)
    return health and context



def _service_of(slots: Any) -> str:
    """从槽位里取服务名（取不到 → 空串）—— 用来判断"这次缺不缺**目标**"。"""
    names = list(getattr(slots, "service_names", ()) or ())
    if names:
        return str(names[0])
    entity = getattr(slots, "service", None)
    if entity is None:
        return ""
    return str(getattr(entity, "name", "") or "")


class ClarifyHub:
    """插件注册表 + 仲裁。"""

    def __init__(self, registry: ClarifyRegistry, *,
                 budget: int = CLARIFY_BUDGET,
                 env_sensitive: Sequence[str] = ENV_SENSITIVE_INTENTS) -> None:
        self.registry = registry
        self.budget = int(budget)
        self.env_sensitive = tuple(env_sensitive)
        #: ★ 本次仲裁里"插件出题失败"的留痕（`decide()` 每轮清空）。
        #:   出题失败 = 这次**本该问却没问**，不能让用户与运维都看不出来。
        self.plugin_failures: list[str] = []

    def take_plugin_failures(self) -> tuple[str, ...]:
        """取走"插件出题失败"的留痕并清空 —— 调用方（路由层）把它并进 `degraded`。"""
        taken = tuple(self.plugin_failures)
        self.plugin_failures.clear()
        return taken

    # ------------------------------------------------------------------
    def requests_from(self, *, intent: str, slots: Any,
                      unresolved: Sequence[str],
                      message: str = "") -> tuple[ClarificationRequest, ...]:
        """从"当前状态"派生候选缺口（把已有的判据收进一个地方）。

        ★ `fallback` 必须写清 —— 不问的时候我们打算怎么办。
        ★★ `message` 用来识别**泛化的健康询问**（"最近是否有问题？"这类**缺目标**的问法）。
          识别到就问一句**带候选**的（用户指示：反问必须给出 AI 自己的建议），
          并显式标 `promote=True` —— 因为这类问法会走 `OUT_OF_SCOPE` 兜底，而兜底默认**不升级**。
        """
        missing = set(str(name) for name in unresolved)
        out: list[ClarificationRequest] = []
        if looks_like_health_scan(message) and not _service_of(slots):
            out.append(ClarificationRequest(
                gap="missing_target",
                why="这是一句**泛化的健康询问**（在问有没有问题），但**没有说是哪个服务**",
                evidence=("识别依据：健康词 + 时间/系统指代",),
                fallback="只能回一句『没有目标』—— 用户问的就是系统的事，那是误导",
                promote=True))
        if "env" in missing and intent in self.env_sensitive:
            out.append(ClarificationRequest(
                gap="env", why="用户原话与请求上下文都没有给出环境",
                fallback="按部署默认环境继续，并在响应里标注「环境未经确认」",
                evidence=(f"intent={intent}",)))
        if "service" in missing:
            out.append(ClarificationRequest(
                gap="service", why="不知道要查哪个服务",
                fallback="按被观测清单里的第一个服务继续（并标注猜测）"))
        if "time_window" in missing:
            out.append(ClarificationRequest(
                gap="time_window", why="用户没给时间窗",
                # ★ 不写死具体默认值：默认窗来自 `slots.yaml::defaults.time_window`
                #   （改配置时这句文案不会跟着变，写死就等于说错话）。
                fallback="按**配置的默认时间窗**继续（并标注这是默认值，不是用户说的）"))
        return tuple(out)

    # ------------------------------------------------------------------
    def decide(self, baseline: ClarificationDecision, *,
               ctx: AskContext,
               requests: Iterable[ClarificationRequest] = ()) -> ClarificationDecision:
        """在"既有判据（baseline）"之上做仲裁，必要时**升级**为一次带 plugin_id 的反问。"""
        self.plugin_failures = []
        if ctx.no_clarify:
            return ClarificationDecision(
                needed=False, reason=("调用方要求 `no_clarify=true` → 本次不澄清，"
                                      "按最佳猜测继续，并在响应里标注「未经澄清」"))
        if ctx.budget_left <= 0:
            return ClarificationDecision(
                needed=False,
                reason=(f"澄清预算用尽（上限 {self.budget}）→ 不再追问，"
                        f"给最佳猜测 + 不确定性说明（不进入死循环）"))
        candidates = self._candidates(requests, ctx)
        if baseline.needed and baseline.questions:
            # 既有判据已经决定要问：给它补上 plugin_id（缺了它 resume 认不出回填方式）
            return self._attach_plugin_id(baseline, candidates, ctx)
        if not candidates:
            return baseline
        return self._promote(candidates, ctx, baseline)

    # ------------------------------------------------------------------
    def _candidates(self, requests: Iterable[ClarificationRequest],
                    ctx: AskContext) -> tuple[ClarificationRequest, ...]:
        """过滤：没插件的、**已经问过的**（同一 `(session, plugin, gap)` 不许重复问）。"""
        asked = set(str(gap) for gap in ctx.asked_gaps)
        out = []
        for request in requests:
            if request.gap in asked:
                continue
            if not self.registry.for_gap(request.gap):
                continue
            out.append(request)
        return tuple(out)

    def _attach_plugin_id(self, baseline: ClarificationDecision,
                          candidates: tuple[ClarificationRequest, ...],
                          ctx: AskContext) -> ClarificationDecision:
        """把 baseline 的每个问题绑到对应插件上（按 `slot` 匹配），问句优先用插件的。"""
        by_gap: dict[str, ClarificationQuestion] = {}
        for request in candidates:
            question = self._question_for(request, ctx)
            if question is not None:
                by_gap[request.gap] = question
        questions: list[ClarificationQuestion] = []
        for question in baseline.questions:
            replacement = by_gap.get(str(question.slot))
            questions.append(replacement if replacement is not None else question)
        return ClarificationDecision(
            needed=True, questions=tuple(questions), reason=baseline.reason,
            expected_cost=baseline.expected_cost, ask_cost=baseline.ask_cost,
            requires_confirmation=baseline.requires_confirmation)

    def _promote(self, candidates: tuple[ClarificationRequest, ...], ctx: AskContext,
                 baseline: ClarificationDecision) -> ClarificationDecision:
        """baseline 说"不用问"，但状态里有**硬缺口** → 按优先级问**一个**（只问一个）。

        ★★ 两条边界（实测踩出来的：把"越界/降级"也拖进反问，会让"帮我写首诗"变成追问）：
          ① **默认只升级 `env`**（`PROMOTE_GAPS`）：`service` 这类缺口，既有的代价判据
             （`SLOT_REQUIREMENTS` 的硬条件）**已经**在需要时问了 —— 这里再来一次
             等于同一件事问两遍；
             ★ `time_window` 不在 `PROMOTE_GAPS` 里（它的问句接不住：问了也不改变本次
             取数口径），所以**没有**注册 `missing_window` 插件 —— 缺了它，未升级的
             缺口连候选都进不来（`_candidates` 按 `registry.for_gap` 过滤）。
          ② OUT_OF_SCOPE默认不补运维槽位；KNOWLEDGE_QA有知识链路，
             但不要求服务/环境槽位，知识指代由qa_agent处理。
          ★★ 收窄例外：缺口的提出方**显式声明 `promote=True`** 时放行（见 `requests_from`
             的"泛化健康询问"）—— 口径从"整体放宽"改成"逐场景声明"。
        """
        if ctx.intent in NO_PROMOTE_INTENTS:
            # ★★ 这两类意图默认不升级，**但显式声明 `promote=True` 的缺口例外** ——
            #   "泛化健康询问"正是要走这条路：它必然落在 `OUT_OF_SCOPE` 兜底上，
            #   而它的问句**接得住**（用户答了服务名，就会重新按 RCA 走一遍）。
            promoted = [item for item in candidates if item.promote]
            if not promoted:
                return baseline
        else:
            promoted = [item for item in candidates
                        if item.gap in PROMOTE_GAPS or item.promote]
        if not promoted:
            return baseline
        # ★ 显式要求升级的优先（它带着具体场景的理由与证据）
        promoted.sort(key=lambda item: (not item.promote, item.gap))
        request = promoted[0]
        question = self._question_for(request, ctx)
        if question is None:
            # ★ 出不了题就不问（fail-closed，不硬问）—— 但**不是静默**：
            #   `_question_for` 已把失败原因记进 `plugin_failures`，调用方会并进 `degraded`。
            return baseline                       # 出不了题就不问（fail-closed，不硬问）
        return ClarificationDecision(
            needed=True, questions=(question,),
            reason=(f"反问插件 `{question.plugin_id}`：{request.why}"
                    f"（不问的话只能「{request.fallback}」—— 该缺口会影响结论，"
                    f"先问一句）"),
            expected_cost=baseline.expected_cost)

    def _question_for(self, request: ClarificationRequest,
                      ctx: AskContext) -> Optional[ClarificationQuestion]:
        """取该缺口下**优先级最高**的插件出题；出不了（或没选项）就不问。

        ★★ 出题失败**必须留痕**：以前是 `except: continue` —— 插件炸了就等于
          "压根没有这个插件"，用户只看到没被追问，排障也无从下手。
          现在两条通道都写：`logger.warning`（排障）+ `plugin_failures`（进响应 `degraded`）。
          行为不变：仍然**不崩、不硬问**，返回 `None` 让调用方照旧回落。
        """
        for plugin in self.registry.for_gap(request.gap):
            plugin_id = str(getattr(plugin, "id", "") or "?")
            try:
                question = plugin.question(request, ctx)
            except Exception as exc:  # noqa: BLE001 —— 插件是替换点：出题失败不拖垮主链路
                logger.warning("反问插件 %s 在 gap=%s 上出题失败（%s: %s）→ 本次不追问",
                               plugin_id, request.gap, type(exc).__name__, exc)
                self.plugin_failures.append(
                    f"反问插件 `{plugin_id}`（gap={request.gap}）出题失败"
                    f"（{type(exc).__name__}: {exc}）→ 本次不追问，按既有兜底继续")
                continue
            if str(getattr(question, "question", "") or "").strip() and \
                    tuple(getattr(question, "options", ()) or ()):
                return question
            # ★ 返回了"渲染不出来的问句"（没文本 / 没选项）同样是一条**静默降级**：
            #   用户看不到这句反问，等于没问 —— 也要留痕（但不许硬问）。
            logger.debug("反问插件 %s 在 gap=%s 上给出了不可用的问句 → 跳过",
                         plugin_id, request.gap)
            self.plugin_failures.append(
                f"反问插件 `{plugin_id}`（gap={request.gap}）没给出可渲染的问句"
                f"（缺问句文本或选项）→ 本次不追问")
        return None

    # ------------------------------------------------------------------
    def advice_from_degraded(self, degraded: Iterable[str], *,
                             error_code: str = "") -> Optional[ClarificationQuestion]:
        """从**降级信号**里认出"下游不可用"，给出"事实 + 手册原文 + 确认命令"的反问。

        ★ 为什么放在仲裁器：这是"要不要打断用户 + 用哪个插件"的**统一判断**，
          不该散在渲染层或取证层（统一出口）。
        ★ 找不到手册条目时**照样出题**，但问题里**明说没有对应条目**（不编建议）。
        """
        lines = [str(line) for line in degraded if str(line).strip()]
        code = str(error_code or "").strip()
        if not code:
            code = detect_error_code(lines)
        if not code:
            return None
        gap = "sdk_unavailable" if code in _SDK_CODES else "tool_failure"
        facts = tuple(line[:120] for line in lines if code in line)[:2] or (f"错误码 {code}",)
        request = ClarificationRequest(
            gap=gap, why=f"下游不可用（{code}）",
            evidence=facts,
            fallback="把已收集到的证据交给用户，并如实说明『没查成』")
        ctx = AskContext(intent="", error_code=code)
        return self._question_for(request, ctx)

    def apply(self, plugin_id: str, answer: Any) -> Optional[SlotPatch]:
        """把用户答案交给对应插件回填（`resume` 用）。认不出来 → None（**不猜**）。"""
        plugin = self.registry.by_id(plugin_id)
        if plugin is None:
            return None
        return plugin.apply(answer)


#: 这些错误码指向"采集 SDK / 下游压根没接通"（手册里对应「端口被占」「没接 SDK」等小节）
_SDK_CODES: tuple[str, ...] = (ErrorCode.TOOL_UNAVAILABLE.value,)
#: 从任意文本里认出错误码（`TOOL_TIMEOUT` / `POOL_INIT_FAILED` …）
_CODE_RE = re.compile(r"\b([A-Z][A-Z0-9_]{4,})\b")


def detect_error_code(lines: Iterable[str]) -> str:
    """从降级信号里挑一个**已知的错误码**（未知的大写 token 不算 —— 不猜）。"""
    known = {str(code) for code in ErrorCode}
    for line in lines:
        for token in _CODE_RE.findall(str(line)):
            if token in known:
                return token
    return ""
