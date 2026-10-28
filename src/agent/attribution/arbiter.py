"""仲裁 —— 把一堆假设收敛成一个决定（`ArbitResult`）。

--------------------------------------------------------------------------------
★ 四条规则
--------------------------------------------------------------------------------

  · **唯一高分 → 结论**：排除被否决的之后，最高分 ≥ `CONFIRM_SCORE`(0.70)
    且**引用可追溯的支持证据** → `concluded`。0.70 是确认阈值；
    "必须有 supporting"意味着结论必须能追到证据。
  · **全部低分 → 拒答**：最高分 < `ABSTAIN_BELOW`(0.30) → `abstain`
    + **必须给 report**。拒答是正确行为，但必须输出
    "已收集证据 + 未排除可能 + 建议人工方向"。
  · **top-2 接近 → 定向取证**：存活假设 ≥2 且 `top1 - top2 < TOP2_CLOSE`(0.15)
    → `need_targeted_evidence` + `open_questions`。softmax 之后"接近"意味着
    证据不足以区分，**再查一轮**比硬选一个诚实。
  · **首选被否决 → 用次高**：★ 这条**不需要单独的分支**：被否决的假设在进入
    比较前就被剔除，`max()` 自然取到次高（但它仍要过 0.70 才能成结论）。
    少一个分支 = 少一处口径分叉。

★ 顺序（没有外部规定，这里定死并给理由）：**"全部低分"优先于"top-2 接近"**。
  0.12 与 0.09 只差 0.03，但两条都低分说明"证据太弱"，而不是"两条难分" ——
  此时去追"能区分它们的证据"很可能一直查不出来，而"拒答 + 缺什么才能定论"更有用。
  只有最高分 ≥ 0.30 时，"接近"才真的意味着"再查一步就能分"。

★ 还有第五种情形：**中间分（0.30 ~ 0.70）且次高差距 ≥ 0.15**。
  它既不该硬下结论、也没有"定向取证"可做 → 同样 `abstain`，
  report 里明确写"分数落在中间区间 + 缺什么才能定论"。
  ★ 把这一条写出来而不是硬塞进上面四条，是因为"没写到的分支"最容易变成静默行为。

--------------------------------------------------------------------------------
★ 三个阈值的含义（它们决定"定论 / 再查一轮 / 拒答"的边界）
--------------------------------------------------------------------------------

  数字本身定义在 `common/thresholds.py`（**唯一来源**，终止判据也读它那一份），
  这里只说它们各自回答什么问题：

  · `CONFIRM_SCORE` = 0.70："证据够不够定根因"的**上界**。也是写入长期记忆的
    同一道门槛 —— 两处必须一起改。
  · `TOP2_CLOSE` = 0.15："两条候选是否难分"。差距小于它就说明证据不足以区分，
    应当**再查一轮**而不是硬选（这是可操作性判据，不是拍脑袋）。
  · `ABSTAIN_BELOW` = 0.30："证据是否整体太弱"的**下界**。8 类候选的 softmax
    均分约 0.125，0.30 约等于"高于均分一倍多，但仍远离 0.70"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Final, Optional, Sequence

from ..common.contracts import ArbitResult, Evidence, Hypothesis
from ..common.enums import ArbitDecision, EvidenceDirection, HypothesisStatus
from ..common.thresholds import ABSTAIN_BELOW, CONFIRM_SCORE, TOP2_CLOSE
from .categories import CATEGORY_LABEL

__all__ = ["arbitrate", "ArbiterThresholds", "DEFAULT_THRESHOLDS", "CONFIDENCE_CEILING",
           "CONFIRM_SCORE", "TOP2_CLOSE", "ABSTAIN_BELOW"]


@dataclass(frozen=True)
class ArbiterThresholds:
    confirm: float = CONFIRM_SCORE
    top2_close: float = TOP2_CLOSE
    abstain_below: float = ABSTAIN_BELOW

    def to_dict(self) -> dict[str, float]:
        return {"confirm": self.confirm, "top2_close": self.top2_close,
                "abstain_below": self.abstain_below}


DEFAULT_THRESHOLDS: Final[ArbiterThresholds] = ArbiterThresholds()


def arbitrate(hypotheses: Sequence[Hypothesis], *,
              thresholds: ArbiterThresholds = DEFAULT_THRESHOLDS,
              missing_signals: Sequence[str] = (),
              evidence: Sequence[Evidence] = (),
              critic_failed: bool = False,
              critic_failure_reason: str = "",
              unverified_hypotheses: Sequence[str] = ()) -> ArbitResult:
    """把假设收敛成一个 `ArbitResult`（**永远有返回**，拒答也是结论）。

    ★ 本函数是**规则流水线**：每一条规则只做"能不能定"，能定就返回、不能定返回 `None`
      让下一条接着判。**调用顺序 = 规则顺序**（见 `_RULES`），所以"顺序"这件事
      在代码里一眼可见，而不是散在 130 行的 if/return 里。
      （本函数曾有 132 行；拆分只搬代码，不改任何判据与文案。）

    ★ `evidence` 是本次黑板上**全部**证据：用来做"结论必须建立在**观测到变化**的证据上"
      这一条判据（见 `_structural_only_reason`）—— 只有结构事实（`direction=flat`
      的依赖图/爆炸半径）时**不能定因**，那是先验，不是证据。

    ★ `unverified_hypotheses` 是 Critic **没能验证成**的假设 id（必要条件所在数据源本轮
      没查成 / 反查工具没查成）。它们**不是被证伪**（所以不进 `EXCLUDED`、照样参与排序），
      但"没验证成"同样**不等于通过了检验** —— 如果**最高分那条**正是其中之一，
      就不许坐实成根因。
    """
    context = _RuleContext.of(
        hypotheses=hypotheses, thresholds=thresholds, missing_signals=missing_signals,
        evidence=evidence, critic_failed=critic_failed,
        critic_failure_reason=critic_failure_reason,
        unverified_hypotheses=unverified_hypotheses)
    for rule in _RULES:
        result = rule(context)
        if result is not None:
            return result
    # ★ 收口规则④：它是**唯一一条不返回 `None` 的规则**，所以不放进 `_RULES`
    #   （放进表里就等于声称"它也可能定不了"，那是假话）。
    return _rule_middle(context)


@dataclass(frozen=True)
class _RuleContext:
    """一条规则需要的**全部输入**（一次组装、逐条传递，避免每条规则都摊 8 个参数）。"""

    all_items: tuple[Hypothesis, ...]
    ranked: tuple[Hypothesis, ...]          # 排除被否决的之后，按分数降序
    thresholds: ArbiterThresholds
    missing_signals: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    unverified: frozenset[str]
    critic_failed: bool
    critic_failure_reason: str

    @classmethod
    def of(cls, *, hypotheses: Sequence[Hypothesis], thresholds: ArbiterThresholds,
           missing_signals: Sequence[str], evidence: Sequence[Evidence],
           critic_failed: bool, critic_failure_reason: str,
           unverified_hypotheses: Sequence[str]) -> "_RuleContext":
        all_items = tuple(hypotheses)
        alive = [item for item in all_items
                 if item.status is not HypothesisStatus.EXCLUDED]
        return cls(
            all_items=all_items,
            ranked=tuple(sorted(alive, key=lambda item: (-item.score, item.hypothesis_id))),
            thresholds=thresholds, missing_signals=tuple(missing_signals),
            evidence=tuple(evidence), unverified=frozenset(unverified_hypotheses),
            critic_failed=bool(critic_failed),
            critic_failure_reason=str(critic_failure_reason or ""))

    @property
    def best(self) -> Hypothesis:
        return self.ranked[0]


def _rule_critic_failed(ctx: _RuleContext) -> Optional[ArbitResult]:
    """规则⓪：反事实检验**没跑成** → 拒答。"""
    #   ★ 理由：Critic 是唯一的反证机制。它没跑成时，"未经检验"绝不等于"通过了检验"
    #     （那会让组件故障反而提高结论的自信度）。宁可少说，不可说错。
    if not ctx.critic_failed:
        return None
    return ArbitResult(
        decision=ArbitDecision.ABSTAIN,
        alternatives=list(ctx.all_items),
        report=("**拒答**（不是失败）：反事实检验（Critic）没有跑成 —— "
                f"{ctx.critic_failure_reason or '未知原因'}。"
                "没有反证就不下结论（结论必须建立在**被检验过**的假设上）。"
                + _evidence_digest(list(ctx.all_items), ctx.missing_signals)),
        reason="Critic 未完成 → 拒答（组件故障应让结论更保守）",
    )


def _rule_nothing_alive(ctx: _RuleContext) -> Optional[ArbitResult]:
    """规则⓪a：所有候选都被否决了 → 拒答。"""
    if ctx.ranked:
        return None
    return ArbitResult(
        decision=ArbitDecision.ABSTAIN,
        alternatives=[],
        report=_abstain_report(list(ctx.all_items), "所有候选假设都被反事实检验否决了",
                               ctx.missing_signals),
        reason="全部候选被排除 → 拒答（拒答是正确行为，但必须给 report）",
    )


def _rule_unverified_top(ctx: _RuleContext) -> Optional[ArbitResult]:
    """规则⓪b：最高分那条**没验证成** → 拒答。"""
    #   ★ 为什么在这里而不是在 Critic 里排除：这两件事必须分开 ——
    #     · **被证伪**（必要条件在查成了的数据源上确实没有）→ 排除，用次高；
    #     · **没验证成**（那个数据源本轮挂了 / 反查工具报错）→ **不排除**，
    #       但也不能当成"通过检验"（否则数据源一挂，剩下的假设无人反对就成根因）。
    #   两条都拒答会掩盖区别、都不拒答会放走假结论，所以：保留候选、但**不坐实**。
    best = ctx.best
    if best.hypothesis_id not in ctx.unverified or best.score < ctx.thresholds.confirm:
        return None
    return ArbitResult(
        decision=ArbitDecision.ABSTAIN,
        alternatives=list(ctx.ranked),
        report=_abstain_report(
            list(ctx.ranked),
            f"最高分 {best.hypothesis_id}（{best.score:.2f}）**没能通过反事实检验**"
            f"——它的必要条件所在数据源本轮没查成（或反查工具没查成），"
            f"所以既不能确认也不能排除", ctx.missing_signals)
        + " ★ 这不是「分数不够」，而是「**少了验证手段**」："
          "重试上面那几路数据源后重跑，就能判定这条到底成不成立。",
        reason=("最高分候选未被检验（数据源缺失/反查没查成）→ 拒答"
                "（没验证成 ≠ 通过了检验）"),
    )


def _rule_concluded(ctx: _RuleContext) -> Optional[ArbitResult]:
    """规则①：唯一高分（且引用可追溯、且**不是只有结构事实**）。"""
    best = ctx.best
    if best.score < ctx.thresholds.confirm or not _has_traceable_support(best):
        return None
    structural = _structural_only_reason(best, ctx.evidence)
    if structural is not None:
        return ArbitResult(
            decision=ArbitDecision.ABSTAIN,
            alternatives=list(ctx.ranked),
            report=_abstain_report(list(ctx.ranked), structural, ctx.missing_signals),
            reason=("最高分够了，但**证据里没有「变化」**（只有结构事实）→ 拒答："
                    "结论必须建立在观测到的异常上"),
        )
    confirmed = best.model_copy(update={"status": HypothesisStatus.CONFIRMED})
    return ArbitResult(
        decision=ArbitDecision.CONCLUDED,
        root_cause=confirmed,
        # ★★ `confidence` **不是** softmax 份额（那个在"只剩一个候选"时
        #   恒等于 1.0，与证据强弱无关，数据源挂得越多反而越自信）。
        #   这里给的是**校准分**：`Prior × Π LR` 归一化前的强度，
        #   按"证据充分度"折算，并保证 < 1.0（softmax 份额另见 `Hypothesis.score`）。
        confidence=_calibrated_confidence(confirmed, ctx.evidence),
        alternatives=[item for item in ctx.ranked[1:]],
        report=(f"根因：{confirmed.statement}（分数 {confirmed.score:.2f} ≥ "
                f"{ctx.thresholds.confirm}，支持证据 {len(confirmed.supporting)} 条）"),
        reason="最高分 ≥ 阈值且未被否决 → 收敛",
    )


def _rule_all_low(ctx: _RuleContext) -> Optional[ArbitResult]:
    """规则②：全部低分 → 拒答。

    ★ 顺序说明（顺序没有外部规定，这里定死并给理由）：
      "全部低分"优先于"top-2 接近"。0.12 与 0.09 虽然只差 0.03，
      但**两条都低分**意味着"证据太弱"，而不是"两条难分" ——
      此时去查"能区分它们的证据"很可能查不出来（甚至一直查下去），
      而回一个"拒答 + 缺什么才能定论"对用户更有用。
      反过来，只要最高分 ≥ ABSTAIN_BELOW，"接近"才真的意味着"再查一步就能分"。
    """
    best = ctx.best
    if best.score >= ctx.thresholds.abstain_below:
        return None
    return ArbitResult(
        decision=ArbitDecision.ABSTAIN,
        alternatives=list(ctx.ranked),
        report=_abstain_report(
            list(ctx.ranked),
            f"最高分 {best.score:.2f} < {ctx.thresholds.abstain_below}（全部低分）",
            ctx.missing_signals),
        reason="全部假设低分 → 拒答",
    )


def _rule_top2_close(ctx: _RuleContext) -> Optional[ArbitResult]:
    """规则③：top-2 接近 → 定向取证。"""
    if len(ctx.ranked) < 2:
        return None
    first, second = ctx.ranked[0], ctx.ranked[1]
    if (first.score - second.score) >= ctx.thresholds.top2_close:
        return None
    return ArbitResult(
        decision=ArbitDecision.NEED_TARGETED_EVIDENCE,
        alternatives=list(ctx.ranked),
        open_questions=_targeted_questions(first, second, ctx.missing_signals),
        report=(f"{first.hypothesis_id} 与 {second.hypothesis_id} 分数接近"
                f"（{first.score:.2f} / {second.score:.2f}）→ 需要区分性证据"),
        reason="top-2 接近 → 定向二次取证",
    )


def _rule_middle(ctx: _RuleContext) -> ArbitResult:
    """规则④：中间分且没有可区分的候选 → 拒答（写明缺什么）。

    ★ **唯一一条不返回 `None` 的规则**（它是收口的那一条）。
    """
    best = ctx.best
    gap = (ctx.ranked[0].score - ctx.ranked[1].score) if len(ctx.ranked) > 1 else 0.0
    return ArbitResult(
        decision=ArbitDecision.ABSTAIN,
        alternatives=list(ctx.ranked),
        report=_abstain_report(
            list(ctx.ranked),
            f"最高分 {best.score:.2f} 落在 [{ctx.thresholds.abstain_below}, "
            f"{ctx.thresholds.confirm}) 的中间区间，且与次高差 {gap:.2f} "
            f"（≥ {ctx.thresholds.top2_close}）不足以靠定向取证翻转", ctx.missing_signals),
        reason=("分数不足以定论、也没有可区分的候选 → 拒答（"
                "输出已收集证据 + 未排除可能 + 建议人工方向）"),
    )


#: ★★ **规则顺序就是这张表**（`arbitrate` 按顺序问，第一个"能定"的说了算），
#:   表走完还没定，就交给收口规则④ `_rule_middle`。把顺序变成数据而不是散落的 if：
#:   想调顺序就调这张表，评审时也只看这一行。
_RULES: Final[tuple[Callable[[_RuleContext], Optional[ArbitResult]], ...]] = (
    _rule_critic_failed,      # ⓪  Critic 没跑成
    _rule_nothing_alive,      # ⓪a 全被否决
    _rule_unverified_top,     # ⓪b 最高分没验证成
    _rule_concluded,          # ①  唯一高分
    _rule_all_low,            # ②  全部低分
    _rule_top2_close,         # ③  top-2 接近
)
#: 校准置信度的上界：★ **不允许 1.0**。
#: 理由：任何一个"从证据算出来的分数"都不该声称"确定" —— 证据本身可能不完整
#: （数据源缺失、窗口太短、只有一条信号）。留一档余量，客户端与用户才不会读到"必然"。
CONFIDENCE_CEILING: Final[float] = 0.95


def _calibrated_confidence(hypothesis: Hypothesis,
                           evidence: Sequence[Evidence]) -> float:
    """把"份额"翻译成一个**不吹牛**的置信度。

    输入与作用：

      · `hypothesis.prior`：先验（`Prior` 恒 < 1.0，本身就是一个保守量）
      · `evidence_likelihoods` 的 log LR 之和：证据净强度（正=支持，负=反对）
      · 支持证据条数 / 是否有"变化方向"的证据：证据充分度

    口径：`min(CEILING, prior × 证据净强度的 sigmoid × 充分度系数)`。
    ★ 它**不是概率**，是"按现有证据敢下多大决心"——字段说明里写清楚。
    """
    import math

    logs = [math.log(lr) for lr in hypothesis.evidence_likelihoods.values() if lr > 0]
    net = sum(logs) if logs else 0.0
    strength = 1.0 / (1.0 + math.exp(-net))                 # (0,1)：净支持越多越接近 1
    support = len(hypothesis.supporting)
    richness = min(1.0, 0.5 + 0.25 * support)                # 1 条 0.75、2 条 1.0
    has_change = any(getattr(getattr(item, "direction", None), "value", "") != "flat"
                     for item in evidence
                     if getattr(item, "evidence_id", "") in set(hypothesis.supporting))
    value = hypothesis.prior * strength * richness * (1.0 if has_change else 0.6)
    return round(min(CONFIDENCE_CEILING, max(0.0, value)), 4)


def _evidence_digest(hypotheses: Sequence[Hypothesis],
                     missing_signals: Sequence[str]) -> str:
    alive = [item for item in hypotheses if item.status is not HypothesisStatus.EXCLUDED]
    text = ""
    if alive:
        text += (" 未排除的可能：" + "、".join(
            f"{item.hypothesis_id}={item.score:.2f}" for item in
            sorted(alive, key=lambda h: -h.score)[:3]) + "。")
    if missing_signals:
        text += f" 缺失信号源：{'、'.join(missing_signals)}（没查成 ≠ 没有异常）。"
    return text


def _has_traceable_support(hypothesis: Hypothesis) -> bool:
    """`confirmed` 必须有支持证据（否则结论无法追溯）。"""
    return bool(hypothesis.supporting)


def _structural_only_reason(hypothesis: Hypothesis,
                            evidence: Sequence[Evidence]) -> Optional[str]:
    """**只有结构事实**时给出拒答理由（`None` = 有"变化"方向的证据，可以定因）。

    ★ 为什么必须有这一条（真链路实测）：`topology_spread` 是唯一有证据的类别时，
      softmax over 1 candidate = **1.0** → 必然 ≥ 0.70 → 于是仅凭
      "order 依赖 inventory、payment" + "爆炸半径：影响 gateway" 这两条**结构事实**
      就给出了"根因"。但依赖图说明的是"谁连着谁"，**不是"出了什么事"** ——
      把它当根因等于用先验当证据。

    判据：支持证据里至少有一条 `direction` 不是 `flat`（`up/down/spike/drop`
    才是"观测到了变化"）。拿不到证据列表时**不放过**（无法核对 → 不敢确认）。
    """
    if not evidence:
        return ("无法核对支持证据的方向（调用方没给证据列表）→ 不敢确认；"
                "结论必须建立在可核对的异常证据上")
    by_id = {item.evidence_id: item for item in evidence}
    directions = []
    for evidence_id in hypothesis.supporting:
        item = by_id.get(evidence_id)
        directions.append(getattr(getattr(item, "direction", None), "value", None))
    if any(value and value != EvidenceDirection.FLAT.value for value in directions):
        return None
    return (f"支持它的 {len(hypothesis.supporting)} 条证据**全是结构事实**"
            f"（direction=flat：依赖图/爆炸半径这类「谁连着谁」）—— "
            f"没有任何**观测到变化**的证据（up/down/spike/drop）。"
            f"结构事实只能做**先验**，不足以定因")


def _targeted_questions(first: Hypothesis, second: Hypothesis,
                        missing_signals: Sequence[str]) -> list[str]:
    questions = [
        (f"{first.hypothesis_id}（{CATEGORY_LABEL.get(first.category, first.category)}）"
         f" 与 {second.hypothesis_id}"
         f"（{CATEGORY_LABEL.get(second.category, second.category)}）分数接近 "
         f"({first.score:.2f} / {second.score:.2f}) —— 需要能**区分**它们的证据"),
    ]
    for signal in first.missing_necessary or second.missing_necessary:
        questions.append(f"缺必要条件 {signal} —— 拿到它才能确认或排除该类假设")
    for signal in missing_signals:
        questions.append(f"{signal} 这一路没查成 —— 需要重试该源"
                         f"（否则这条信号是空的，不是「没有异常」）")
    return questions


def _abstain_report(hypotheses: Sequence[Hypothesis], why: str,
                    missing_signals: Sequence[str]) -> str:
    """拒答必须输出三件事：已收集证据 / 未排除可能 / 建议人工方向。"""
    alive = [item for item in hypotheses if item.status is not HypothesisStatus.EXCLUDED]
    excluded = [item for item in hypotheses if item.status is HypothesisStatus.EXCLUDED]
    lines = [f"**拒答**（不是失败）：{why}。"]
    if alive:
        lines.append("未排除的可能：" + "、".join(
            f"{item.hypothesis_id}={item.score:.2f}（{len(item.supporting)} 条支持）"
            for item in sorted(alive, key=lambda h: -h.score)[:3]))
    else:
        lines.append("未排除的可能：（无）")
    if excluded:
        lines.append("已排除：" + "；".join(
            f"{item.hypothesis_id}（{item.excluded_reason}）" for item in excluded[:3]))
    if missing_signals:
        lines.append(f"缺失信号源：{'、'.join(missing_signals)}"
                     f"（**没查成 ≠ 没有异常**）")
    lines.append("建议人工方向：按未排除的候选逐个核对变更记录与依赖拓扑；"
                 "或补充上面缺的必要证据后重跑。")
    return " ".join(lines)
