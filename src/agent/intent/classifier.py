"""意图结果的**校准**与**澄清判据**。

--------------------------------------------------------------------------------
★ 为什么"校准"必须单独存在
--------------------------------------------------------------------------------

`IntentResult` 里有两个置信度：

| 字段 | 谁说的 | 谁在用 |
|---|---|---|
| `confidence_raw` | 判定方（规则表 / 模型）自己的分数 | **只用于解释与复盘** |
| `confidence` | **校准后**的可信度 | 澄清判据、记忆写入闸门（<0.7 拒写）、风险分级 |

把两者设成同一个值是最常见的偷懒，代价很实在：
**模型自评普遍偏高**（实测动不动就给 0.95），于是所有"置信度 < 0.7 才澄清 /
才拒写"的门槛**全部失效** —— 门槛还在，但永远不会被触发。

所以这里做三件可解释、单调、有界的事：

1. **规则层**：`rules.py` 的分数是"命中证据权重和 ÷ 饱和值(2.0)"，它是**证据强度**、
   不是概率。用单调线性映射压到 `[0.35, 0.90]`，并**封顶 0.90** ——
   词表匹配永远存在"这句话里有这些词、但意思不同"的可能，不该有 0.99 的规则判定。
2. **模型层**：自评向 0.5 **收缩**（×0.80）并封顶 0.95 ——
   校准的常见做法就是"把过度自信往中间拉"，同时**明确不到 1.0**。
3. **一致加成**：规则与模型指向同一意图时 +0.05（两条独立线索一致本身就是证据）。
   ★ 但 **`REMEDIATION` 不给这个加成** —— 写操作宁可多想一步，
     "两条线索都说是重启"不构成"可以少问一句"的理由。

--------------------------------------------------------------------------------
★ 澄清判据：不是"置信度低就问"
--------------------------------------------------------------------------------

判据是「**猜错代价 × 概率 > 问一句成本**」。落地成两步：

1. **硬条件优先**：意图的**关键槽位缺失**时**必问**，不做代价比较
   （"排查哪个服务"没答，后面无事可做；此时省一句问话毫无意义）。
   - 关键槽位按意图定：RCA / 查指标 / 处置都要知道**实体**；
     变更影响是"变更单号**或**服务"至少给一个（两者都没有就无从谈起）。
2. **否则才做代价比较**：`(1 - confidence) × 猜错代价(意图) > 问一句成本(1.0)` 才问，
   且问的是**意图确认**（"我理解你是想排查故障根因，对吗？"）。

★ 为什么代价表写在**代码**里而不是 YAML：它和 `DEFAULT_ACTION_LEVEL`（风险等级）
  是同一类东西 —— **策略与安全口径**。这类东西被一个 YAML 改动就能放宽，
  等于给"自动提权"留了后门（同 `rules.py` 里"外置证据表、不动裁决逻辑"的边界）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping, Optional, Sequence

from ..common.contracts import Slots
from ..common.enums import Intent, IntentSource

__all__ = [
    "Calibration",
    "calibrate",
    "ClarificationQuestion",
    "ClarificationDecision",
    "decide_clarification",
    "decide_unrecognized",
    "looks_on_topic",
    "SYSTEM_SIGNAL_SLOTS",
    "SLOT_REQUIREMENTS",
    "MISTAKE_COST",
    "ASK_COST",
    "MAX_QUESTIONS",
    "INTENT_LABELS",
]

# ======================================================================
#  一、校准
# ======================================================================
#: 规则表兜底分（`rules.yaml` 的 `fallback_score`）—— 映射的**下端点**
_RULE_RAW_FLOOR: Final[float] = 0.20
#: 规则层校准后的取值区间。★ 上限刻意不是 1.0（词表匹配永远有歧义余地）
_RULE_CONF_FLOOR: Final[float] = 0.35
_RULE_CONF_CEIL: Final[float] = 0.90

#: 模型层：自评向 0.5 收缩的系数（越小越保守）
_MODEL_SHRINK: Final[float] = 0.80
_MODEL_CONF_FLOOR: Final[float] = 0.05
_MODEL_CONF_CEIL: Final[float] = 0.95

#: 规则与模型一致时的加成（★ 高危意图除外，见 docstring）
_AGREE_BONUS: Final[float] = 0.05


@dataclass(frozen=True)
class Calibration:
    """校准结果 + **一句话解释**（解释要能进日志/响应，否则调不动阈值）。"""

    confidence: float
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {"confidence": self.confidence, "note": self.note}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _as_source(source: "IntentSource | str") -> str:
    return source.value if isinstance(source, IntentSource) else str(source)


def calibrate(
    raw: float,
    *,
    source: "IntentSource | str",
    intent: Intent,
    agrees_with_rules: bool = False,
) -> Calibration:
    """把**原始分数**映射成**校准后的置信度**（单调、有界、可解释）。

    Args:
        raw: 判定方自己的分数（规则层的证据强度 / 模型的自评）。
        source: `rule` / `small_model` / `large_model` / `fallback`。
        intent: 用于"高危意图不加成"这条例外。
        agrees_with_rules: 规则层与模型层是否指向同一意图。
    """
    raw_value = _clamp(float(raw), 0.0, 1.0)
    src = _as_source(source)

    if src == IntentSource.VECTOR_MATCH.value:
        # Cosine similarity is evidence for routing, not a calibrated probability.
        return Calibration(confidence=round(min(.9, .5 + .4 * raw_value), 4),
                           note=f"示例向量相似度 {raw_value:.2f}；门槛与意图间分差已通过，非概率")

    if src == IntentSource.FALLBACK.value:
        # ★ 兜底判定**不做映射**：它就是要低到"让模型有充分理由翻盘"，
        #   给它一个好看的校准值反而会掩盖"其实没判出来"这件事。
        return Calibration(
            confidence=round(raw_value, 4),
            note=f"兜底（fallback）：保持原分 {raw_value:.2f} —— 明确表示"
                 f"「没有足够证据」，不做美化",
        )

    if src == IntentSource.RULE.value:
        span = 1.0 - _RULE_RAW_FLOOR
        ratio = 0.0 if span <= 0 else (raw_value - _RULE_RAW_FLOOR) / span
        confidence = _RULE_CONF_FLOOR + _clamp(ratio, 0.0, 1.0) * (
            _RULE_CONF_CEIL - _RULE_CONF_FLOOR
        )
        note = (
            f"规则层：证据强度 {raw_value:.2f} 线性映射到 "
            f"[{_RULE_CONF_FLOOR:.2f}, {_RULE_CONF_CEIL:.2f}]"
            f"（证据强度不是概率；上限 {_RULE_CONF_CEIL:.2f} 是刻意的）"
        )
    else:
        confidence = 0.5 + (raw_value - 0.5) * _MODEL_SHRINK
        confidence = _clamp(confidence, _MODEL_CONF_FLOOR, _MODEL_CONF_CEIL)
        note = (
            f"模型层：自评 {raw_value:.2f} 向 0.5 收缩 ×{_MODEL_SHRINK:.2f}"
            f"（实测模型自评普遍偏高），上限 {_MODEL_CONF_CEIL:.2f}"
        )

    if agrees_with_rules and intent is not Intent.REMEDIATION:
        confidence = min(_MODEL_CONF_CEIL, confidence + _AGREE_BONUS)
        note += f"；+{_AGREE_BONUS:.2f}（规则与模型一致，两条独立线索互相印证）"
    elif agrees_with_rules:
        note += "；★ 规则与模型一致，但高危意图（REMEDIATION）**不给加成**"

    return Calibration(confidence=round(confidence, 4), note=note)


# ======================================================================
#  二、澄清判据
# ======================================================================
#: 问一句的成本（归一化基准）。所有"猜错代价"都是相对它的倍数。
ASK_COST: Final[float] = 1.0

#: 猜错一次的代价（按意图）。★ 与 `DEFAULT_ACTION_LEVEL` 同类的**策略数据**，
#: 刻意留代码里：能被一个 YAML 改动放宽的安全口径，等于留了自动提权的后门。
MISTAKE_COST: Final[Mapping[Intent, float]] = {
    Intent.OUT_OF_SCOPE: 0.2,     # 判成越界 → 用户重说一句即可
    Intent.KNOWLEDGE_QA: 0.4,     # 已显式降级为拒答，代价有限
    Intent.METRIC_QUERY: 0.6,     # 查错指标 → 答非所问，但无副作用
    Intent.CHANGE_IMPACT: 1.0,    # 影响面判断错 → 可能放行不该发的变更
    Intent.INCIDENT_RCA: 1.8,     # 排查错方向 → 白跑一轮 + 结论错
    Intent.REMEDIATION: 3.0,      # 动生产：猜错代价最高
}

#: 关键槽位：每个元组是"**其中任意一个**给了就算满足"。
#: 空元组 = 该意图不依赖具体实体（拒答 / 知识问答）。
SLOT_REQUIREMENTS: Final[Mapping[Intent, tuple[tuple[str, ...], ...]]] = {
    Intent.INCIDENT_RCA: (("service",),),
    Intent.METRIC_QUERY: (("service",),),
    Intent.CHANGE_IMPACT: (("change_id", "service"),),
    Intent.REMEDIATION: (("service",),),
    Intent.KNOWLEDGE_QA: (),
    Intent.OUT_OF_SCOPE: (),
}

#: ★★ **必须硬性确认的意图**（不等价于"猜错代价高"）。
#:
#: 为什么单列一张表，而不是继续用 `MISTAKE_COST`：
#: 代价模型权衡的是"猜错的期望损失 vs 问一句的成本"，它**天然会**在
#: 「置信度高 → 不问」这一侧收敛；可对**破坏性动作**来说，"自信"根本不是放行的理由
#: —— 用户说"把 order 重启一下"时，规则层置信度校准后是 0.69，
#: `(1-0.69)×3.0 = 0.92 ≤ ASK_COST(1.0)` → 旧实现**不问**（实测复现），
#: 而同一个模型对一个**只读**提问却会追问 —— 方向正好反了。
#: 安全门的判据不该是成本，所以这类意图**与"槽位缺失"同级**：不问就往下走是不允许的。
CONFIRM_ALWAYS: Final[frozenset[Intent]] = frozenset({Intent.REMEDIATION})

#: 一次最多问几个问题 —— 追问是成本，"一口气问 5 个"等于把活推回给用户
MAX_QUESTIONS: Final[int] = 2

INTENT_LABELS: Final[Mapping[Intent, str]] = {
    Intent.INCIDENT_RCA: "排查故障根因",
    Intent.METRIC_QUERY: "查看指标现状",
    Intent.CHANGE_IMPACT: "评估变更影响",
    Intent.REMEDIATION: "咨询处置建议或提出处置动作",
    Intent.KNOWLEDGE_QA: "咨询运维知识",
    Intent.OUT_OF_SCOPE: "与运维无关的问题",
}

_SLOT_LABELS: Final[Mapping[str, str]] = {
    "service": "服务",
    "change_id": "变更单号",
    "time_window": "时间范围",
    "symptom": "异常现象",
    "env": "环境",
}


@dataclass(frozen=True)
class ClarificationQuestion:
    """一个可展示的追问（客户端要能把 `questions` 直接渲染出来）。"""

    slot: str
    question: str
    options: tuple[str, ...] = ()
    #: ★★ 这句反问是**哪个插件**提的 —— `resume` 靠它决定答案怎么回填。
    #:   缺省空串 = 旧的"把答案拼成一句话"路径（向后兼容，老客户端不受影响）。
    plugin_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"slot": self.slot, "question": self.question}
        if self.options:
            out["options"] = list(self.options)
        if self.plugin_id:
            out["plugin_id"] = self.plugin_id
        return out


@dataclass(frozen=True)
class ClarificationDecision:
    """要不要问、问什么、**为什么**（reason 要能直接进日志，便于调阈值）。"""

    needed: bool
    questions: tuple[ClarificationQuestion, ...] = ()
    reason: str = ""
    expected_cost: float = 0.0
    ask_cost: float = ASK_COST
    #: ★ 这是**动作确认门**（不是"信息不足"）：编排/执行层据此走审批，
    #: 而不是把 `unlocked_tools` 里有 `change_write` 当成"可以动手了"。
    requires_confirmation: bool = False

    def to_payload(self) -> Optional[dict[str, Any]]:
        """转成 `IntentResult.clarification` 的形状（不需要澄清时给 `None`）。"""
        if not self.needed or not self.questions:
            return None
        payload: dict[str, Any] = {
            "questions": [q.to_dict() for q in self.questions],
            "reason": self.reason,
            "expected_cost": round(self.expected_cost, 4),
            "ask_cost": self.ask_cost,
        }
        if self.requires_confirmation:
            # ★ 只在"是确认门"时出现这个键 —— 缺了它才表示"普通的信息补充"。
            payload["requires_confirmation"] = True
        return payload


def _filled(slots: Slots, name: str) -> bool:
    value = getattr(slots, name, None)
    if name == "service":
        return slots.service is not None or bool(slots.service_names)
    if name == "env":
        return value is not None
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def _missing_requirements(intent: Intent, slots: Slots) -> list[tuple[str, ...]]:
    """返回**未被满足**的关键槽位组（每组里任意一个给了就算满足）。"""
    return [
        group
        for group in SLOT_REQUIREMENTS.get(intent, ())
        if not any(_filled(slots, name) for name in group)
    ]


def _question_for(intent: Intent, group: tuple[str, ...],
                  service_options: Sequence[str]) -> ClarificationQuestion:
    """为缺槽位生成一个**具体**的问题（不能只回"信息不足"）。"""
    if group == ("service",) or group == ("change_id", "service"):
        options = tuple(service_options)
        if len(group) == 2:
            text = ("你想评估哪一次变更的影响？请给**变更单号**，"
                    "或直接说受影响的**服务**。")
        else:
            text = f"你想查哪个{_SLOT_LABELS['service']}？"
        if options:
            text += f"（当前观测到：{'、'.join(options)}）"
        return ClarificationQuestion(slot="service", question=text, options=options)
    first = group[0]
    label = _SLOT_LABELS.get(first, first)
    return ClarificationQuestion(slot=first, question=f"请补充{label}。")


def _question_intent(intent: Intent) -> ClarificationQuestion:
    label = INTENT_LABELS.get(intent, intent.value)
    return ClarificationQuestion(
        slot="intent",
        question=(f"我理解你是想「{label}」，对吗？"
                  f"如果不是，请直接说明你想要什么。"),
    )


# ======================================================================
#  「未识别」与「越界」必须分开（实测修订）
# ======================================================================
#: 抽到任意一个，就说明用户**在问与系统运行相关的事**（而不是闲聊/越界）。
#: ★ 判据刻意用"抽到的槽位"，因为它是**可复核的事实**（不是猜语气）。
SYSTEM_SIGNAL_SLOTS: Final[tuple[str, ...]] = (
    "service", "service_names", "symptom", "time_window", "change_id", "slo_target",
)


def looks_on_topic(slots: Slots, *, unresolved: Sequence[str] = ()) -> list[str]:
    """输入里有哪些"**用户真的说了**的系统相关信号"（空列表 = 与系统运行无关）。

    ★★ 为什么必须传 `unresolved`：时间窗等槽位会被**填默认值**（`30m`），
      而"没说"与"说了默认值"是靠 `unresolved` 区分的。
      如果只看"字段有没有值"，`time_window` 永远有值 →
      **连"帮我写一首诗"都会被判成"与系统相关"** → 兜底再也不拒答（实测踩到）。
      所以判据是"填了 **且不在 unresolved 里**"。
    """
    missing = {str(name) for name in unresolved}
    hits: list[str] = []
    for name in SYSTEM_SIGNAL_SLOTS:
        if name in missing:
            continue
        value = getattr(slots, name, None)
        if name == "service_names":
            if value:
                hits.append(name)
        elif value not in (None, "", [], {}):
            hits.append(name)
    return hits


def _question_unrecognized(service: Optional[str],
                           service_options: Sequence[str]) -> ClarificationQuestion:
    """「没听懂」时问的那一句：用**已抽到的实体**把问题问具体（不泛泛地说"信息不足"）。

    ★ `service` 为空时用 `service_options`（用户这一句里提到的服务清单）说出来 ——
      多服务时列清单，而不是把范围收窄成第一个。
    """
    options = tuple(service_options)
    if not service and len(options) == 1:
        service = options[0]
    if service:
        text = (f"这句我没判定出你想做什么。你是想**排查「{service}」的故障**，"
                f"还是**看一下它现在的指标**？")
    else:
        text = ("这句我没判定出你想做什么。你是想**排查某个服务的故障**，"
                "还是**看一下它现在的指标**？")
        if options:
            text += f"（当前观测到：{'、'.join(options)}）"
    return ClarificationQuestion(slot="intent", question=text, options=options)


def decide_unrecognized(
    slots: Slots,
    *,
    service_options: Sequence[str] = (),
    reason: str = "",
    unresolved: Sequence[str] = (),
) -> ClarificationDecision:
    """兜底（**未识别**）时的处置：与系统相关 → 转澄清；真的无关 → 交给越界拒答。

    ★ 为什么不能一律回"超出我的职责范围"：
      规则未定案 + 模型不可用时，`OUT_OF_SCOPE` 是**兜底值**，但它同时是"越界类"。
      于是一个**完全合规的运维提问**（如「order 老是等数据库，是不是池子被人动过」）
      会被回一句"这个问题超出了我的职责范围" —— 用户被告知自己的运维问题
      "不关运维的事"，而真实原因只是"这句我没听懂"。
      这一层还污染「**拒答正确性**」指标（分母里混进了"没听懂"）。

    ★ 与 `decide_clarification` 的区别：那边做的是**代价比较**（猜错代价 × 出错概率 > 问一句成本）；
      这里不比较 —— 因为"未识别"时**没有**可猜的结论，问一句永远优于乱回一句。
    """
    signals = looks_on_topic(slots, unresolved=unresolved)
    if not signals:
        return ClarificationDecision(
            needed=False,
            reason=(f"未识别，且输入里**没有任何用户明说的系统相关信号**"
                    f"（{reason or '规则未命中'}）→ 按越界拒答处理"
                    f"（这是「真的无关」，不是「我没听懂」）"),
        )
    service = None
    if slots.service is not None:
        service = slots.service.name
    elif len(slots.service_names) == 1:
        service = slots.service_names[0]
    # ★★ 多个服务时**不取第 0 个**：那是把"用户要一起看这几个"静默改成"看第一个"，
    #   而清单的第 0 个固定是 gateway —— 问全局却问成"你是要排查 gateway 吗"。
    #   多服务时改用一句覆盖全部服务的问法（下面 `_question_unrecognized` 会列清单）。
    return ClarificationDecision(
        needed=True,
        questions=(_question_unrecognized(service, service_options),),
        reason=(f"未识别（{reason or '规则未定案且模型不可用'}），"
                f"但输入里抽到了系统相关信号 {signals} → **转为定向澄清**，"
                f"而不是回「超出我的职责范围」"),
        expected_cost=0.0,
    )


def _question_confirm(intent: Intent) -> ClarificationQuestion:
    """破坏性动作的**确认问句**（不是"信息不足"，是"你确定要动手吗"）。"""
    label = INTENT_LABELS.get(intent, intent.value)
    return ClarificationQuestion(
        slot="confirm",
        question=(f"你确认要{label}吗？"
                  f"我会先出**预案与回滚方案**、定风险等级，"
                  f"需要审批的动作会等你批准后才落地 —— 回「确认」我才继续。"),
    )


def decide_clarification(
    intent: Intent,
    confidence: float,
    slots: Slots,
    *,
    service_options: Sequence[str] = (),
) -> ClarificationDecision:
    """按"猜错代价 × 概率 > 问一句成本"决定要不要澄清。

    ★ 三个易错点：

      1. **不是"置信度低就问"**：`OUT_OF_SCOPE` / `KNOWLEDGE_QA` 置信度再低也不问
         —— 它们本来就走拒答/降级路径，问了也接不住。
      2. **缺关键槽位是硬条件**：此时**不做**代价比较（不问就没法继续），
         否则"高置信地知道用户想排查故障、但不知道是哪个服务"会直接放行。
      3. ★★ **破坏性动作（`CONFIRM_ALWAYS`）也是硬条件**：代价模型会在
         "越自信越不问"这一侧收敛，而安全门的判据不该是成本（实测复现）。
    """
    confidence_value = _clamp(float(confidence), 0.0, 1.0)
    expected = (1.0 - confidence_value) * MISTAKE_COST.get(intent, 1.0)

    # 越界不追问；知识问答无需服务槽位，交由qa_agent处理知识与指代。
    if intent in (Intent.OUT_OF_SCOPE, Intent.KNOWLEDGE_QA):
        return ClarificationDecision(
            needed=False,
            reason=('知识问答不要求服务槽位，交由qa_agent检索与回答'
                    if intent is Intent.KNOWLEDGE_QA else '明确越界/拒绝路径不追问服务槽位'),
            expected_cost=expected,
        )

    # ---- 硬条件①：关键槽位缺失（先问目标，再问确认）----
    missing = _missing_requirements(intent, slots)
    questions = [
        _question_for(intent, group, service_options)
        for group in missing[:MAX_QUESTIONS]
    ]
    # ---- 硬条件②：破坏性动作必须确认（**在代价比较之前**，与槽位缺失同级）----
    must_confirm = intent in CONFIRM_ALWAYS
    if must_confirm:
        questions.append(_question_confirm(intent))

    if questions:
        reasons: list[str] = []
        if missing:
            reasons.append("关键槽位缺失（"
                           + "；".join("/".join(group) for group in missing)
                           + "）—— 不问就没法继续，**这一步不做代价比较**")
        if must_confirm:
            reasons.append(
                f"{intent.value} 属破坏性动作类 → **必须确认**（与槽位缺失同级，"
                f"不走代价比较）：代价模型只在『猜错损失 vs 问一句成本』之间权衡，"
                f"而安全门的判据不该是成本（否则越自信越不问 —— 实测）")
        return ClarificationDecision(
            needed=True,
            questions=tuple(questions[:MAX_QUESTIONS]),
            reason="；".join(reasons),
            expected_cost=expected,
            requires_confirmation=must_confirm,
        )

    if expected > ASK_COST:
        return ClarificationDecision(
            needed=True,
            questions=(_question_intent(intent),),
            reason=(
                f"猜错代价 {MISTAKE_COST.get(intent, 1.0):.1f} × 出错概率 "
                f"{1.0 - confidence_value:.2f} = {expected:.2f} > 问一句成本 "
                f"{ASK_COST:.1f} → 值得确认一次"
            ),
            expected_cost=expected,
        )

    return ClarificationDecision(
        needed=False,
        reason=(
            f"代价比较不成立（{expected:.2f} ≤ {ASK_COST:.1f}）："
            f"猜错的期望损失低于问一句的成本，直接按 {intent.value} 继续"
        ),
        expected_cost=expected,
    )
