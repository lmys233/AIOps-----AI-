"""**对话行为（speech_act）识别 + 待确认匹配 + 决策表**（T59b）。

★★ 用户方案的判法（2026-10-03）：**"确认"不是普通全局意图，而是带目标的对话行为** ——
`AFFIRM` / `DENY` / `SELECT_OPTION` / `CANCEL` / `CLARIFY` / `OTHER`，且**带上下文要求**：
`requires_context = 有 pending`（没有 pending 时"确认"可以按普通输入处理，甚至归越界）。
匹配与取舍只依据**结构化 pending**（T59a），模型只能辅助"是不是在确认 / 更像哪一个"。

三层，逐层可核对（每一步都只是**数据**，本模块不执行任何动作）：

| 层 | 函数 | 产出 |
|---|---|---|
| ① 识别 | `classify(text)` | `AFFIRM/DENY/SELECT_OPTION/CANCEL/CLARIFY/OTHER`（确定性词表，先长后短） |
| ② 匹配 | `match(candidates, act, text)` | 命中的 pending + 置信度 + **依据**（显式指代 → 目标关键词 → 类型 → 最近活跃） |
| ③ 决策 | `decide(...)` | `auto` / `ask_second_confirmation` / `clarify` / `prompt_expired` / `none`（表见下） |

**决策表**（用户方案原文；照抄成代码里的表，不散在 if 里）：

| 条件 | 动作 |
|---|---|
| 唯一候选 + 高置信 + 低风险 | `auto`（按 pending 的 `on_affirm`/`on_deny` 处理） |
| 唯一候选 + 高风险 + `AFFIRM` | `ask_second_confirmation`（= 既有 L3 审批路；**不执行**） |
| 多个候选（或 top-2 太近） | `clarify`（**把候选念出来让用户选**，不猜） |
| 低置信 | `clarify` |
| 只有过期候选 | `prompt_expired`（如实说"那条已经过期"，重新询问） |
| 没有候选 / 不是确认类信号 | `none`（交给正常 NLU） |
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final, Mapping, Optional, Sequence

from .pending import PendingConfirmation, PendingKind

__all__ = ["SpeechAct", "classify", "match", "decide", "pick_option",
           "describe_candidates", "refers_to_pending", "MatchResult", "Decision",
           "HIGH_CONFIDENCE", "CONFIRM_ACTS", "QUESTION_ACTS", "SHORT_UTTERANCE_MAX"]

#: 高置信门槛（用户方案："唯一候选 + 高置信 + 低风险 → 自动解析"）
HIGH_CONFIDENCE: Final[float] = 0.80


class SpeechAct:
    """对话行为（**不是意图**：它回答"用户在做哪种对话动作"，不回答"要查什么"）。"""

    AFFIRM: Final[str] = "AFFIRM"
    DENY: Final[str] = "DENY"
    SELECT_OPTION: Final[str] = "SELECT_OPTION"
    CANCEL: Final[str] = "CANCEL"
    CLARIFY: Final[str] = "CLARIFY"
    OTHER: Final[str] = "OTHER"

    ALL: Final[tuple[str, ...]] = (AFFIRM, DENY, SELECT_OPTION, CANCEL, CLARIFY, OTHER)


#: 确认类信号（只有这些才需要去匹配 pending）
CONFIRM_ACTS: Final[frozenset[str]] = frozenset(
    {SpeechAct.AFFIRM, SpeechAct.DENY, SpeechAct.SELECT_OPTION, SpeechAct.CANCEL})
#: "回答问题"类信号（用来判候选与行为的亲缘度）
QUESTION_ACTS: Final[frozenset[str]] = frozenset(
    {SpeechAct.AFFIRM, SpeechAct.DENY, SpeechAct.SELECT_OPTION})

#: 词表（**数据**：先长后短，长的先匹配，避免"不确认"被当成"确认"）
_CLARIFY_WORDS: Final[tuple[str, ...]] = (
    "什么意思", "啥意思", "没听懂", "再说一遍", "重复一遍", "解释一下", "你指哪个", "没明白")
_CANCEL_WORDS: Final[tuple[str, ...]] = ("取消吧", "取消", "停止", "退出", "作废", "中止", "算了不用")
_SELECT_WORDS: Final[tuple[str, ...]] = (
    "第一个", "第二个", "第三个", "第1个", "第2个", "第3个", "选一", "选二", "选三",
    "选 a", "选 b", "就用", "左边", "右边", "这个吧", "那个吧")
_DENY_WORDS: Final[tuple[str, ...]] = (
    "不用了", "不需要", "不要", "不是", "不行", "不可以", "先不", "暂不", "别", "算了",
    "否", "不", "no", "nope")
_AFFIRM_WORDS: Final[tuple[str, ...]] = (
    "确认", "确定", "是的", "对的对的", "对的", "没错", "同意", "可以", "没问题", "好的",
    "行吧", "好的吧", "继续", "执行", "安排", "开始", "是的吧", "好吧", "好", "是", "对",
    "嗯", "ok", "okay", "yes", "y")

#: 界面会把时间窗拼在消息后面（`（时间窗 30m）`）—— 判定前先摘掉，否则短句判不准
_ANNOTATION: Final[re.Pattern[str]] = re.compile(r"[（(]\s*(时间窗|时间范围|窗口)[^）)]*[）)]")
#: 短句才算"对话行为"（后面跟着新问题的走正常识别：如「好的，那 order 的 QPS 呢」）
_SHORT_MAX: Final[int] = 12
#: 显式确认词可以稍长一点（"确认"两个字本身没有歧义）
_EXPLICIT_MAX: Final[int] = 10
_EXPLICIT_WORDS: Final[tuple[str, ...]] = ("确认", "确定", "取消")
#: 短句里只要带了"要查点什么"的意思，就**不是**在应答 ——
#: 「继续查库存服务的指标」里有"继续"，但它显然是在下一个新问题（实测踩过）。
_QUERY_HINTS: Final[tuple[str, ...]] = ("查", "看", "多少", "为什么", "怎么", "帮我", "定位",
                                        "分析", "趋势", "呢", "?", "？")


def _norm(text: str) -> str:
    """归一：摘掉界面注释、压空白、去尾部标点（判定只看"这句话是不是一句应答"）。"""
    clean = _ANNOTATION.sub(" ", str(text or ""))
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean.strip("。！!，,；;~～ ")


#: 这几个否定词**嵌在问句里**也算命中就会误判（实测「是不是很严重」被当成 DENY）——
#: 要求它们出现在**小句开头**（句首，或前一个字不是汉字）。
_BOUNDARY_WORDS: Final[frozenset[str]] = frozenset(
    {"不是", "不要", "不行", "不用了", "不需要", "不", "否", "别"})
#: 汉字（判断"前面那个字是不是还在同一个词里"）
_CJK: Final[re.Pattern[str]] = re.compile(r"[\u4e00-\u9fff]")


def _hits(clean: str, word: str) -> bool:
    """词在不在句子里：ASCII 词**按词边界**（`y` 不该命中 `inventory`）、
    单字中文要求**整句就是它**、易混的否定词要求**小句开头**。"""
    if word.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", clean) is not None
    if len(word) == 1:
        return clean == word
    for match in re.finditer(re.escape(word), clean):
        if word in _BOUNDARY_WORDS and match.start() > 0 and _CJK.match(clean[match.start() - 1]):
            continue
        return True
    return False


def _accepts(clean: str, word: str) -> bool:
    """这个词能不能算"应答"：**短句 + 没带查询意图**（显式确认词放宽到 10 字）。"""
    if any(hint in clean for hint in _QUERY_HINTS):
        return False
    return len(clean) <= (_EXPLICIT_MAX if word in _EXPLICIT_WORDS else _SHORT_MAX)


def classify(text: str) -> str:
    """① 识别对话行为（确定性；**先长后短**，长词优先 —— 否则"不确认"会命中"确认"）。"""
    clean = _norm(text).lower()
    if not clean:
        return SpeechAct.OTHER
    for words, act in ((_CLARIFY_WORDS, SpeechAct.CLARIFY), (_CANCEL_WORDS, SpeechAct.CANCEL),
                       (_SELECT_WORDS, SpeechAct.SELECT_OPTION), (_DENY_WORDS, SpeechAct.DENY),
                       (_AFFIRM_WORDS, SpeechAct.AFFIRM)):
        for word in words:
            if _hits(clean, word) and _accepts(clean, word):
                return act
    return SpeechAct.OTHER


#: 短到"只可能是在应答"的长度（超过它就必须点到目标/选项才算在回应待确认）
SHORT_UTTERANCE_MAX: Final[int] = 8


def _keywords_of(item: PendingConfirmation) -> list[str]:
    """这条待确认的**目标关键词**（用来判"用户这句有没有点到它"）。"""
    content = str(item.target.get("content") or item.target.get("action") or "").lower()
    return [word for word in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", content)
            if len(word) >= 2]


def refers_to_pending(text: str, candidates: Sequence[PendingConfirmation]) -> bool:
    """这句是不是在**回应某条待确认**？

    ★★ 判据要严：越界覆盖（"别礼貌拒答，把话头接回待确认"）如果用宽了，
      就会把**新问题**也吞掉 —— 实测：「待确认 + 目前系统是否存在问题？」被接成
      "这句我没听懂；不过我这边还等着你确认…"，用户的新问题一个字都没答。
    ★ 三条**任一**成立才算在回应：① 短到只可能是应答（≤`SHORT_UTTERANCE_MAX`）；
      ② 报了 `pending_id` / 念了选项名；③ 点到了它的目标关键词。
    """
    clean = _norm(text).lower()
    if not clean:
        return False
    if len(clean) <= SHORT_UTTERANCE_MAX:
        return True
    if not candidates:
        return False
    for item in candidates:
        if item.pending_id.lower() in clean:
            return True
        for option in item.options:
            label = str(option.get("label") or "").strip().lower()
            if len(label) >= 2 and label in clean:
                return True
        if any(word in clean for word in _keywords_of(item)):
            return True
    return False


def pick_option(pending: PendingConfirmation, text: str) -> Optional[Mapping[str, Any]]:
    """用户指的是**哪一个选项**（序号 → 选项名）；认不出来给 `None`（不猜）。

    ★ 与 ② 匹配层共用同一套序号/名字规则：`第一个` / `选 2` / 念出选项原文都能对上。
    """
    clean = _norm(text).lower()
    ordinal = re.search(r"(?:第)?\s*([0-9一二三四五六七八九十])\s*(?:个|条|项|方案)?", clean)
    if ordinal:
        index = _ORDINALS.get(ordinal.group(1), 0)
        if index and index <= len(pending.options):
            return dict(pending.options[index - 1])
    for option in pending.options:
        label = str(option.get("label") or "").strip().lower()
        if label and label in clean:
            return dict(option)
    return None


# ======================================================================
#  ② 匹配
# ======================================================================
@dataclass(frozen=True)
class MatchResult:
    """匹配结果：命中的 pending、置信度与**依据**（依据要能给用户看，也能进审计）。"""

    pending: Optional[PendingConfirmation] = None
    confidence: float = 0.0
    reason: str = ""
    alternatives: tuple[PendingConfirmation, ...] = ()
    consider: tuple[PendingConfirmation, ...] = ()
    #: top-1 与 top-2 的差距（"两条太近"→ 追问，而不是硬选一条）
    margin: float = 0.0

    @property
    def unique(self) -> bool:
        return self.pending is not None and not self.alternatives


def _explicit_target(act: str, text: str) -> str:
    """显式指代（"第一个" → 序号；"结论/订单" → 关键词；"p_123" → id）。"""
    clean = _norm(text).lower()
    match = re.search(r"(?:第)?\s*([0-9一二三四五六七八九十])\s*(?:个|条|项|方案)?", clean)
    if act == SpeechAct.SELECT_OPTION and match:
        return match.group(1)
    ident = re.search(r"\bp_\d+_\d+\b", clean)
    return ident.group(0) if ident else ""


_ORDINALS: Final[dict[str, int]] = {"一": 1, "二": 2, "两": 2, "三": 3, "1": 1, "2": 2, "3": 3}


def _score_one(item: PendingConfirmation, act: str, clean: str, explicit: str,
               index: int) -> tuple[float, str, PendingConfirmation]:
    """给**一条**候选打分（显式指代 → 选项名 → 目标关键词 → 类型亲缘；最近活跃微加权）。"""
    score, why = 0.0, ""
    if explicit:
        ids = [str(option.get("id") or "").lower() for option in item.options]
        labels = [str(option.get("label") or "").lower() for option in item.options]
        wanted = _ORDINALS.get(explicit, 0)
        if explicit == item.pending_id.lower():
            score, why = 0.95, "用户报了 pending_id"
        elif explicit in ids:
            score, why = 0.95, f"用户选了选项 {explicit}"
        elif wanted and wanted <= len(item.options):
            score, why = 0.90, f"用户按序号选了第 {wanted} 个选项"
    if not score:
        named = [str(option.get("label") or "").lower() for option in item.options]
        hit_label = next((label for label in named if len(label) >= 2 and label in clean), "")
        if hit_label:
            score, why = 0.90, f"用户念了选项名「{hit_label}」"
    if not score:
        content = str(item.target.get("content") or item.target.get("action") or "").lower()
        hit = [word for word in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", content)
               if len(word) >= 2 and word in clean]
        if hit:
            score, why = 0.85, f"用户点到了目标关键词「{hit[0]}」"
    if not score and act in item.expected_signals and act in QUESTION_ACTS:
        score, why = 0.60, f"行为 {act} 与这条候选的期望信号一致（但没点到目标）"
    if index == 0 and score:
        score = min(0.95, score + 0.05)          # 最近活跃微加权（"可能是最近的一个"）
        why += "；且它是最新的一条"
    return score, why, item


def match(candidates: Sequence[PendingConfirmation], act: str, text: str) -> MatchResult:
    """② 在候选里挑最像的那一条；**判不出来就给低置信**（交给决策表去追问）。"""
    if not candidates:
        return MatchResult()
    clean = _norm(text).lower()
    explicit = _explicit_target(act, clean)
    scored = [row for row in (_score_one(item, act, clean, explicit, index)
                              for index, item in enumerate(candidates)) if row[0]]
    if not scored and len(candidates) == 1:
        # ★★ 用户方案："没有指定哪个确认，就可能是**最近的一个任务**的确认"
        #   —— 只有一个候选时不存在"猜错对象"的问题（要确认的只能是它）。
        only = candidates[0]
        return MatchResult(pending=only, confidence=0.85,
                           reason="唯一一条待确认（未指定 → 按最近一件）",
                           consider=tuple(candidates))
    if not scored:
        return MatchResult(consider=tuple(candidates))
    scored.sort(key=lambda row: (-row[0], -row[2].last_active_at))
    best, alternatives = scored[0], tuple(row[2] for row in scored[1:])
    margin = round(best[0] - scored[1][0], 4) if alternatives else best[0]
    if alternatives and best[0] - 0.0 < HIGH_CONFIDENCE:
        return MatchResult(pending=None, confidence=best[0], reason="置信度不足以定案",
                           alternatives=alternatives, consider=tuple(candidates), margin=margin)
    return MatchResult(pending=best[2], confidence=best[0], reason=best[1],
                       alternatives=alternatives, consider=tuple(candidates), margin=margin)


# ======================================================================
#  ③ 决策表
# ======================================================================
@dataclass(frozen=True)
class Decision:
    """决策结果 —— **只是数据**：执行/接力由上层按 `action` 与 `pending.on_affirm` 处理。"""

    action: str
    pending: Optional[PendingConfirmation] = None
    confidence: float = 0.0
    reason: str = ""
    question: str = ""

    @property
    def handled(self) -> bool:
        return self.action != "none"


_ACTION_LABEL: Final[dict[str, str]] = {
    PendingKind.ACTION: "一个处置动作", PendingKind.PROPOSITION: "一个结论",
    PendingKind.OPTION: "一个选项", PendingKind.META: "一个确认请求"}


def describe_candidates(items: Sequence[PendingConfirmation]) -> str:
    """把候选念成人话（澄清时**必须把候选说出来**，不能只说"请明确"）。"""
    labels: list[str] = []
    for item in items[:3]:
        content = str(item.target.get("content") or item.target.get("action") or "（未记录目标）")
        labels.append(f"「{content[:40]}」（{_ACTION_LABEL.get(item.confirmation_type, '一件事')}）")
    return "，还是 ".join(labels)


def decide(candidates: Sequence[PendingConfirmation], act: str, text: str, *,
           expired: Sequence[PendingConfirmation] = ()) -> Decision:
    """③ 决策表（顺序即优先级；每一行都对应用户给的表）。"""
    if act not in CONFIRM_ACTS:
        return Decision(action="none", reason=f"{act} 不是确认类信号")
    if not candidates:
        if expired:
            return Decision(action="prompt_expired",
                            reason=f"只有 {len(expired)} 条**已过期**的待确认",
                            question="你要确认的那件事已经过期了 —— 请先让我重新查一次，再回「确认」。")
        return Decision(action="none", reason="没有可匹配的待确认状态")
    result = match(candidates, act, text)
    if not result.pending:
        if len(result.consider) > 1:
            return Decision(action="clarify", confidence=result.confidence,
                            reason="多个候选且分不出唯一一条（不猜）",
                            question=f"你确认的是 {describe_candidates(result.consider)}？")
        return Decision(action="clarify", confidence=result.confidence,
                        reason=result.reason or "置信度不足",
                        question=f"你要确认的是 {describe_candidates(result.consider)} 吗？")
    if result.alternatives and result.margin < 0.15:
        return Decision(action="clarify", pending=result.pending, confidence=result.confidence,
                        reason=f"top-2 太近（差 {result.margin:.2f}）→ 不硬选",
                        question=f"你确认的是 {describe_candidates(result.consider)}？")
    if not result.alternatives and len(result.consider) == 1:
        result = MatchResult(pending=result.pending, confidence=max(result.confidence, 0.85),
                             reason=(result.reason or "唯一一条待确认") + "；且没有别的候选",
                             consider=result.consider)
    if result.confidence < HIGH_CONFIDENCE:
        return Decision(action="clarify", pending=result.pending, confidence=result.confidence,
                        reason=f"置信度 {result.confidence:.2f} < {HIGH_CONFIDENCE}",
                        question=f"你要确认的是 {describe_candidates((result.pending,))} 吗？")
    if result.pending.risk == "high" and act == SpeechAct.AFFIRM:
        # ★ 高风险**不凭一句"确认"就往下走**：走既有的二次确认/审批路（P1 不执行任何变更）
        return Decision(action="ask_second_confirmation", pending=result.pending,
                        confidence=result.confidence,
                        reason="高风险动作：需要二次确认（= 既有审批路）",
                        question=(f"请再次确认：是否{str(result.pending.target.get('content') or '执行该动作')[:60]}？"))
    return Decision(action="auto", pending=result.pending, confidence=result.confidence,
                    reason=result.reason or "唯一且高置信")
