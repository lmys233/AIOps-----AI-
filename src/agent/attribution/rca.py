"""RCA Agent —— 候选假设生成 + 先验 + 对数空间打分。

--------------------------------------------------------------------------------
★ 这个 Agent 的**权限边界**
--------------------------------------------------------------------------------

它**只看得见黑板上的证据**，看不到任何原始遥测。这在代码里的体现是：

· 本模块的入参只有 `evidence` / `slots` / `round_no`（外加只读的槽位）；
· 它**不导入**任何数据源（`tools/sources.py`）、也不持有 `ToolExecutor`；
· 可用的工具集由 `enums.AGENT_TOOL_MATRIX["rca_agent"]` 限定为
  `topology_read` / `memory_read`（都是"读整理过的结果"，不是查原始遥测）。

★ "目录即权限"：取证 Agent 在 `agents/`（能碰遥测），归因 Agent 在 `attribution/`
（只能碰证据）。想越界的写法会立刻显得别扭 —— 这比写在文档里让人自觉有效。

--------------------------------------------------------------------------------
★ 两步打分（先验 → 对数空间 + softmax）
--------------------------------------------------------------------------------

```
Prior(H)     = Σ_c w_c · strength_c · affinity[H.category][c]        ← 加权平均，不截断
log Score(H) = log Prior(H) + Σ_i log LR(E_i | H)
Score(H)     = softmax over all hypotheses
```

三个必须写清楚的口径：

1. **`strength_c` 是"这次事故里该特征有多强"**（0~1，来自证据本身）：
   `change` = 有没有"早于异常 onset"的变更（时域因果，晚于 onset 一律 0）；
   `memory` = 记忆来源证据的最高置信度；`topology` = 拓扑来源证据的最高置信度；
   `base_rate` = 1.0（类别的"常见程度"由 `affinity` 承载）。
2. **`affinity` 是"这类假设与该特征有多相关"**（0~1，见 `likelihoods.py`）。
   ★ 两项相乘而不是相加，是为了让"特征很弱"时先验也被压低，而不是被常量顶起来。
3. **`Prior` 恒 < 1.0**：`likelihoods._validate()` 在 import 期断言每个类别的
   `Σ w_c · affinity < 1.0`（先验饱和 = 退化成"只看似然比"，是要避免的）。
   另外给一个**下限** `PRIOR_FLOOR`：`Prior = 0` 会让 `log` 变 `-inf`、
   把整条假设从 softmax 里抹掉 —— 那等于"没证据就出局"，而我们只是想让它排在后面。
   ★ 用了下限会在 `explanation` 里如实说明（不静默）。

--------------------------------------------------------------------------------
★ 为什么生成的是"8 大类"的候选，而不是让模型自由发挥
--------------------------------------------------------------------------------

自由生成的假设**没法比较**（每条假设的证据集不同、类别不同，分数不在同一量纲上），
而评测要的是 **Top-1 / Top-3 命中率**。用固定的类别骨架生成候选，
才能保证"同一场事故里的候选彼此可比、可排序、可复现"。
类别骨架与证据词表的对应关系只有一份（`CATEGORY_SIGNALS`）。

--------------------------------------------------------------------------------
★★ 主语（服务维度）**只有一个事实来源**：候选集的地板
--------------------------------------------------------------------------------

打分框架回答的是"**哪一类**故障"，它不回答"**哪个服务**"。所以实体不在这里自己挑：
调用方把**候选集给出的主语服务**（代码枚举的地板，见 `error_facts`）传进来，
`score(subject_service=…)` 就用它当 `root_entity`。

· 传了（有候选集）→ 实体**必须**是它；打分公式、阈值、类别词表**一律不动**；
· 没传（没候选 / trace 取不到）→ 与以前**逐字一致**（证据自带的服务优先）。

★ 为什么必须由外面传：同一次排查里"叙事主语"与 `root_cause` 的实体是同一个问题，
  在本模块里再算一遍就是**两套口径** —— 真机实测踩到过：叙事已经落到
  `inventory`，而 `root_cause` 还停在 `order`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Final, Optional, Sequence, Tuple

from ..common.contracts import EntityRef, EntityType, Evidence, Hypothesis
from ..common.enums import EvidenceSource, HypothesisStatus
from .categories import CATEGORY_LABEL, CATEGORY_SIGNALS
from .likelihoods import NEUTRAL_LR, PRIOR_WEIGHTS, likelihood_for, rules_of

__all__ = ["RcaAgent", "compute_prior", "prior_components", "CATEGORY_PRIOR_AFFINITY",
           "PRIOR_FLOOR", "MIN_PRIOR", "STRENGTH_MIN_EVIDENCE", "PriorBreakdown",
           "anomaly_onset", "statement_for"]

#: 先验下限（见模块头口径 3）。取 0.05 而不是 0：`log 0.05 ≈ -3.0`，
#: 足以让"毫无先验"的假设明显排在后面，又不至于把它从 softmax 里彻底抹掉。
PRIOR_FLOOR: Final[float] = 0.05

#: `score()` 输出里低于它的候选不返回（避免 8 个类别全塞给仲裁）
MIN_PRIOR: Final[float] = 0.0

#: ★★ "证据不足"伪候选的**虚拟对数分**。
#:
#: 为什么必须有它：softmax 只在**候选之间**分配概率质量。只剩一个候选时
#: `exp(0)/exp(0) = 1.0` —— 分数与证据强弱完全无关，于是
#: "数据源挂得越多 → 能匹配的类别越少 → 置信度越高"，激励方向是反的。
#: 放一个固定强度的"证据不足"进去占一份权重，单候选就无法独吞全部质量。
#: 取值依据：`log(8.0)=2.08` 是"一条最强证据"的量级，这里取 **−0.7**
#: （≈ `Prior` 上限 0.6 的对数），意思是"只有先验、没有像样证据"的兜底强度。
_INSUFFICIENT_LOG_SCORE: Final[float] = -0.7

#: 伪候选在输出里的 key（写在 `last_scores` 里供响应解释"为什么和不是 1.0"；
#: **不会**进黑板、**不会**被仲裁当成根因）。
INSUFFICIENT_ID: Final[str] = "__insufficient__"

#: 特征强度的"证据条数 → 强度"换算：1 条 = 0.6，2 条 = 0.8，3 条及以上 = 1.0。
#: ★ 为什么不是"线性 = 条数/3"：单一来源重复采样（同一条查询的多条证据）
#:   不该把强度顶到满格；用递减收益更贴近"多一条独立佐证"的真实含义。
STRENGTH_MIN_EVIDENCE: Final[tuple[tuple[int, float], ...]] = ((1, 0.6), (2, 0.8), (3, 1.0))


#: ★ 类别 × 先验特征 的**亲和度**（0~1）：四个分项各自"与这类假设多相关"。
#:
#:   · `change`：这类故障有多可能由**变更**引起
#:   · `memory`：历史上这类故障有多少相似案例（记忆命中越像越相关）
#:   · `topology`：这类故障在多大程度上是**拓扑位置**问题
#:   · `base_rate`：这类故障的基础发生率（常见的给高分）
#:
#: ★★ 这是**未校准的初始值**：本仓没有根因标注集，
#:    所以这里只做"公式落地 + 可解释"，**没有**做"在标注集上校准"。
#:    校准入端到端评测（那需要先有带标签的故障集）。
#:    ★ 改这张表会直接改变"哪类假设排前面" —— 与 LR 表同理，必须过 review。
CATEGORY_PRIOR_AFFINITY: Final[dict[str, dict[str, float]]] = {
    "pool_contention": {"change": 0.35, "memory": 0.30, "topology": 0.15, "base_rate": 0.60},
    "downstream_latency": {"change": 0.25, "memory": 0.30, "topology": 0.55, "base_rate": 0.55},
    "downstream_error": {"change": 0.30, "memory": 0.30, "topology": 0.60, "base_rate": 0.45},
    "self_slowness": {"change": 0.20, "memory": 0.30, "topology": 0.10, "base_rate": 0.50},
    "error_burst": {"change": 0.25, "memory": 0.25, "topology": 0.20, "base_rate": 0.55},
    "change_induced": {"change": 0.95, "memory": 0.35, "topology": 0.25, "base_rate": 0.35},
    "capacity_surge": {"change": 0.20, "memory": 0.25, "topology": 0.30, "base_rate": 0.50},
    "topology_spread": {"change": 0.20, "memory": 0.20, "topology": 0.95, "base_rate": 0.25},
}


def _check_affinity() -> None:
    """import 期校验：类别齐备、取值在 [0,1]、**每个类别的先验恒 < 1.0**（不饱和）。"""
    missing = sorted(set(CATEGORY_SIGNALS) - set(CATEGORY_PRIOR_AFFINITY))
    extra = sorted(set(CATEGORY_PRIOR_AFFINITY) - set(CATEGORY_SIGNALS))
    if missing or extra:
        raise AssertionError(
            f"CATEGORY_PRIOR_AFFINITY 与 CATEGORY_SIGNALS 不一致：缺 {missing}、多 {extra}")
    for category, affinity in CATEGORY_PRIOR_AFFINITY.items():
        if set(affinity) != set(PRIOR_WEIGHTS):
            raise AssertionError(
                f"{category} 的亲和度缺少分项："
                f"{sorted(set(PRIOR_WEIGHTS) - set(affinity))}")
        bad = {k: v for k, v in affinity.items() if not 0.0 <= v <= 1.0}
        if bad:
            raise AssertionError(f"{category} 的亲和度越界：{bad}")
        ceiling = sum(PRIOR_WEIGHTS[key] * affinity[key] for key in PRIOR_WEIGHTS)
        if ceiling >= 1.0:
            # ★ 这一条就是"先验不饱和"的**结构性保证**：
            #   四项全满足时 Prior = ceiling，必须仍 < 1.0。
            raise AssertionError(
                f"{category} 的先验上限 {ceiling:.3f} ≥ 1.0 —— 先验会饱和，"
                f"加权平均不截断（四项全满足也必须 < 1.0）")


_check_affinity()


# ======================================================================
@dataclass
class PriorBreakdown:
    """先验的可解释分解（进 `explanation`：为什么这条假设排在前面）。"""

    prior: float
    strengths: dict[str, float]
    contributions: dict[str, float]
    floored: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"prior": round(self.prior, 4),
                "strengths": {k: round(v, 3) for k, v in self.strengths.items()},
                "contributions": {k: round(v, 4) for k, v in self.contributions.items()},
                "floored": self.floored}


def _strength_from_count(count: int) -> float:
    value = 0.0
    for threshold, strength in STRENGTH_MIN_EVIDENCE:
        if count >= threshold:
            value = strength
    return value


def prior_components(evidence: Sequence[Evidence], *, anomaly_onset: Any = None
                     ) -> dict[str, float]:
    """算这次事故的四个**特征强度**（0~1）。与"哪类假设"无关 —— 那是 affinity 的事。"""
    changes = [item for item in evidence if item.source is EvidenceSource.CHANGES]
    # ★ 时域因果：变更必须**早于**异常 onset 才算数
    aligned = [item for item in changes
               if anomaly_onset is None or item.onset <= anomaly_onset]
    memory = [item for item in evidence if item.source is EvidenceSource.MEMORY]
    topology = [item for item in evidence if item.source is EvidenceSource.TOPOLOGY]
    return {
        "change": _strength_from_count(len(aligned)),
        "memory": (max((item.confidence for item in memory), default=0.0)
                   if memory else 0.0),
        "topology": (max((item.confidence for item in topology), default=0.0)
                     if topology else 0.0),
        # 基础发生率是常量特征：类别的"常见程度"由 affinity 承载
        "base_rate": 1.0,
    }


def compute_prior(category: str, strengths: dict[str, float]) -> PriorBreakdown:
    """`Prior(H) = Σ_c w_c · strength_c · affinity[H.category][c]`（加权平均，不截断）。"""
    affinity = CATEGORY_PRIOR_AFFINITY.get(category, {})
    contributions = {key: PRIOR_WEIGHTS[key] * strengths.get(key, 0.0)
                     * affinity.get(key, 0.0) for key in PRIOR_WEIGHTS}
    raw = sum(contributions.values())
    floored = raw < PRIOR_FLOOR
    return PriorBreakdown(prior=max(PRIOR_FLOOR, raw), strengths=dict(strengths),
                          contributions=contributions, floored=floored)


# ======================================================================
@dataclass
class RcaAgent:
    """RCA Agent：从黑板证据生成候选假设并打分。**实现 `HypothesisScorer` 协议。**"""

    #: 低于这个分数的候选不返回（默认全留：让仲裁看到完整分布）
    min_prior: float = MIN_PRIOR
    #: 观测窗口内的"异常 onset"：默认取最早的非变更证据 onset（见 `anomaly_onset`）
    anomaly_onset: Any = None
    #: ★ 先验被 `PRIOR_FLOOR` 截断的类别（上一次 `score()` 的结果；供响应如实说明）
    floored_categories: set[str] = field(default_factory=set)
    #: 上一次 softmax 的份额（含伪候选占掉的那一份；排障用）
    last_scores: dict[str, float] = field(default_factory=dict)
    #: ★★ 上一次每个类别的先验**分解**（`PriorBreakdown.to_dict()` 曾经是死代码，
    #: 而"先验被下限截断"这件事只在内部算过、从没进过响应 → 仍然静默）。
    #: 现在它进 `explanation.prior.by_category`，让"为什么这条候选排前面"可以逐项核对。
    last_priors: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def score(self, *, evidence: Sequence[Evidence], slots: Any,
              round_no: int = 1, subject_service: str = "") -> list[Hypothesis]:
        """从黑板证据生成候选假设并打分（先验 + 对数空间 softmax）。

        ★ 本函数是**流程骨架**（曾有 75 行）：① 聚证据 → ② 每类算候选
          → ③ softmax 归一 → ④ 记录可解释产物。每一步的细节在下面的私有函数里。

        ★★ **状态先清、再判早退**。
          空证据时本函数直接 `return []`（不进 `_finalize`），而 `floored_categories` /
          `last_priors` / `last_scores` 是**进程级单例**上的字段，渲染层
          （`orchestration/facts.py::_prior_explanation`）读的正是它们 ——
          于是"本次"响应里的先验分解与"被下限截断的类别"其实是**上一次请求**的：
          又一次"机器可核对的误导性声明"（只修 `_finalize` 里的 `clear` 不够，
          因为早退这条路径**根本走不到** `_finalize`）。

        Args:
            subject_service: ★ 候选集给出的**主语服务**（代码枚举的地板）。
                传了就用它当每个假设的 `root_entity`（服务维度的唯一事实来源）；
                没传（老路径 / 没有候选）时行为与传入前**逐字一致**。
        """
        self.floored_categories.clear()
        self.last_scores.clear()
        self.last_priors = {}
        items = [item for item in evidence if item is not None]
        if not items:
            return []
        by_category = _group_by_category(items)
        if not by_category:
            return []
        strengths = prior_components(items, anomaly_onset=anomaly_onset(items))
        entity = _root_entity(slots, items, subject_service=subject_service)
        raw = {category: _candidate_for(category, items, entity, strengths)
               for category in by_category}
        return self._finalize(raw)

    # ------------------------------------------------------------------
    def _finalize(self, raw: dict[str, "_RawCandidate"]) -> list[Hypothesis]:
        """③ softmax 归一 + ④ 记录"先验分解 / 被下限截断的类别"，再按门槛筛。

        ★★ `floored_categories` 以前只 `add` 从不 `clear`，而打分器是
          **进程级单例** → 集合里累积的是**所有历史请求**被下限截断过的类别，
          响应里的 `priors.floored_categories` / `floor_note` 会声称"**本次**"某类别
          被 `PRIOR_FLOOR` 抬过 —— 一条可机器核对的**误导性声明**。
          ★ `score()` 的**早退路径**到不了这里，所以重置以 `score()` 为准
          （这一行保留：本方法被直接调用时同样不能留上一次的痕迹）。
        """
        self.floored_categories.clear()
        #   ★ 伪候选"证据不足"也参与 softmax —— 见 `_INSUFFICIENT_LOG_SCORE`。
        _apply_softmax(raw, self.last_scores)
        out = [item[0] for item in raw.values()]
        out.sort(key=lambda hypothesis: (-hypothesis.score, hypothesis.hypothesis_id))
        # ★ 先验被下限截断的候选要**说出来**（算了不输出 = 仍然静默）
        self.last_priors = {category: breakdown.to_dict()
                            for category, (_cand, _log, breakdown) in raw.items()}
        for candidate in out:
            if raw[candidate.category][2].floored:
                self.floored_categories.add(candidate.category)
        return [item for item in out if item.prior >= self.min_prior]

    def snapshot(self) -> dict[str, Any]:
        """**本次** `score()` 的可解释产物快照。

        ★ 为什么必须有这个出口：`last_priors` / `floored_categories` 挂在**进程级单例**
          （`api/wiring.py` 每次 `create_app` 只建一个 `Orchestrator` → 整个进程共用一个
          `RcaAgent`）。而打分（`hypotheses_node._score_candidates`）与渲染
          （`orchestration/facts.py::_prior_explanation`）之间隔着**多次 await**
          （Critic 的工具调用、检查点、会话记忆）→ 两个并发请求会在同一组实例字段上
          互相覆盖，A 的响应里写的是 **B 的先验分解**（而 `hypotheses` 仍是 A 自己的）。

        ★ 为什么"入口 `clear()`"不够：那只治**顺序**调用 —— A 清完、B 又填。
          产物必须**随本次请求带走**：调用方在 `score()` 返回后的**同一个同步块**里
          取一份快照（中间没有 await，不会被别的请求插进来），渲染方只读那份快照。
        """
        return {
            "by_category": {category: dict(breakdown or {})
                            for category, breakdown in (self.last_priors or {}).items()},
            "floored_categories": sorted(self.floored_categories),
        }


#: `score()` 里"一个类别"的中间产物：`(候选, 原始对数分, 先验分解)`
_RawCandidate = Tuple[Hypothesis, float, PriorBreakdown]


def _group_by_category(items: Sequence[Evidence]) -> dict[str, list[Evidence]]:
    """按类别聚拢证据（类别骨架见 `CATEGORY_SIGNALS`）。"""
    by_category: dict[str, list[Evidence]] = {}
    for item in items:
        for category in _categories_of(item.signal):
            by_category.setdefault(category, []).append(item)
    return by_category


def _candidate_for(category: str, items: Sequence[Evidence], entity: EntityRef,
                   strengths: dict[str, float]) -> _RawCandidate:
    """算**一个类别**的候选假设：先验 + Σ log LR → 原始对数分（`score` 留 0 待 softmax 填）。"""
    breakdown = compute_prior(category, strengths)
    log_score = math.log(breakdown.prior)
    likelihoods, supporting, contradicting = _evidence_terms(category, items)
    log_score += sum(math.log(value) for value in likelihoods.values())
    necessary, missing = _necessary_terms(category, items)
    candidate = Hypothesis(
        hypothesis_id=f"H-{category}",
        statement=_statement(category, entity, supporting, missing),
        category=category,
        root_entity=entity,
        prior=round(breakdown.prior, 6),
        evidence_likelihoods=likelihoods,
        score=0.0,                       # ③ softmax 里填
        supporting=sorted(supporting),
        contradicting=sorted(contradicting),
        necessary_evidence=necessary,
        missing_necessary=missing,
        status=HypothesisStatus.CANDIDATE,
    )
    return candidate, log_score, breakdown


def _evidence_terms(category: str, items: Sequence[Evidence]
                    ) -> tuple[dict[str, float], list[str], list[str]]:
    """这条证据对该类别的似然比（`(likelihoods, supporting, contradicting)`）。

    ★★ 用**全部**黑板证据打分，而不是"命中本类别关键词的那些"：
      每条 AGAINST 规则引用的都是**别的类别**的信号（`self_slowness` 反对
      `pool_waiting`、`error_burst` 反对 `downstream_error_rate`……）。
      只看命中项 → 13/13 条反对规则**全部不可达** → `contradicting` 恒为空
      → 任何证据都只会"支持"、绝不"反对"，可反证性整体失效。
    """
    likelihoods: dict[str, float] = {}
    supporting: list[str] = []
    contradicting: list[str] = []
    for item in items:
        lr, _note = likelihood_for(category, item.signal,
                                   getattr(item.direction, "value", None))
        if abs(lr - NEUTRAL_LR) < 1e-9:
            continue
        likelihoods[item.evidence_id] = lr
        (supporting if lr > NEUTRAL_LR else contradicting).append(item.evidence_id)
    return likelihoods, supporting, contradicting


def _necessary_terms(category: str, items: Sequence[Evidence]
                     ) -> tuple[list[str], list[str]]:
    """必要条件：该类别里标了 `necessary` 的规则，有没有对应证据 → `(全部, 缺的)`。"""
    necessary: list[str] = []
    missing: list[str] = []
    for rule in rules_of(category):
        if not rule.necessary:
            continue
        necessary.append(rule.signal)
        if not any(rule.signal in item.signal for item in items):
            missing.append(rule.signal)
    return necessary, missing


def anomaly_onset(evidence: Sequence[Evidence]) -> Any:
    """这次事故的"异常起点" = 最早的非变更证据 onset。**全仓唯一实现。**

    ★ 变更证据**不参与**：它的时间戳常早于异常（这正是"变更引起故障"的样子），
      拿它当基准会把"早于异常的变更"判成"不早于" —— 因果方向就反了。
    ★ 编排层曾经另有一份逐字相同的实现（`Orchestrator._anomaly_onset`）——
      两处只要漂移一次，"Critic 的时域判据"与"先验的时域加权"就会用不同的基准。
      现在只有这里一份，编排层调它。
    """
    onsets = [item.onset for item in evidence
              if item.source is not EvidenceSource.CHANGES]
    return min(onsets) if onsets else None


def _apply_softmax(raw: dict[str, _RawCandidate],
                   sink: Optional[dict[str, float]] = None) -> None:
    """把 `log Score` 归一成 softmax 分数（**就地**写回 `Hypothesis.score`）。

    ★★ 分母里**始终含一个"证据不足"伪候选**：
      否则只剩一个候选时 `score` 恒为 1.0（与证据强弱无关），
      而且"数据源挂得越多 → 候选越少 → 越自信"，激励方向完全反了。
    """
    if not raw:
        return
    logs = {category: item[1] for category, item in raw.items()}
    logs[INSUFFICIENT_ID] = _INSUFFICIENT_LOG_SCORE
    top = max(logs.values())
    # ★ 减最大值是数值稳定的标准做法：exp 不会溢出，softmax 结果不变
    weights = {category: math.exp(value - top) for category, value in logs.items()}
    total = sum(weights.values()) or 1.0
    for category, (candidate, _log_score, _breakdown) in raw.items():
        candidate.score = round(weights[category] / total, 6)
    if isinstance(sink, dict):
        # ★ 伪候选占掉的那一份也留下来（供响应解释"为什么分数不是 1.0"）
        sink.clear()
        sink.update({category: round(value / total, 6)
                     for category, value in weights.items()})


def _categories_of(signal: str) -> tuple[str, ...]:
    """这条信号属于哪几类假设（**与 `likelihoods.category_of` 同一实现**，只此一份）。

    ★ 这里与 `likelihoods.category_of` 曾经逐字重复 —— 两处只要漂移一次，
      "谁能聚到哪条证据"就会不一致。现在只有 `likelihoods` 那一份。
    """
    from .likelihoods import category_of

    return category_of(signal)


def _statement(category: str, entity: EntityRef, supporting: Sequence[str],
               missing: Sequence[str]) -> str:
    return statement_for(category, entity.name, supporting, missing)


def statement_for(category: str, service: str, supporting: Sequence[str],
                  missing: Sequence[str]) -> str:
    """假设陈述句（**全仓唯一一份**：打分时与"主语对齐后重写陈述"时共用）。

    ★ 为什么必须共用：对齐层要把实体换成候选集选中的服务，若它自己拼一句
      "X 的「某类」假设"，两处措辞就会漂移 —— 而这句话是**报告与记忆里的正文**，
      读的人无从核对哪一份才算数。
    """
    label = CATEGORY_LABEL.get(category, category)
    text = (f"{service} 的「{label}」假设：{len(supporting)} 条证据支持"
            f"（{', '.join(list(supporting)[:3]) or '无'}）")
    if missing:
        text += f"；★ 缺必要条件 {list(missing)}"
    return text


def _root_entity(slots: Any, evidence: Sequence[Evidence] = (),
                 *, subject_service: str = "") -> EntityRef:
    """假设的根实体：**候选集选中的主语服务**优先，其次有证据的那个服务，
    再其次槽位里点名的，最后不发明服务名。

    ★★ 为什么要看证据（不是只看槽位）：槽位里的 `service_names` 是"这次要一起看的
      服务"，从中取第 0 个等于把"N 个服务"静默收窄成清单里的第一个（本仓头一个是
      gateway）—— 于是"整个系统是否正常"这类提问的候选假设全部挂在 gateway 上。
      证据自带 `entity`（**报这条错的那个服务**），那才是"谁出事了"的事实来源。
    ★ 顺序：断点 span 的服务 > 有**自身**错误的服务（最早的先） > 槽位点名的 >
      清单第一个（只在只有一个服务时） > 明确标注 `unknown`。
    ★★ `subject_service` 非空 = **这一轮有候选集**（诊断口径里的主语由代码枚举，
      见模块头）。它**压过**上面全部启发式：候选集是查出来的事实，启发式只是兜底。
    """
    from .errors import audit_errors

    if str(subject_service or "").strip():
        return EntityRef.make(EntityType.SERVICE.value, str(subject_service).strip(),
                              getattr(slots, "cluster", None))
    audit = audit_errors(evidence, slots=slots)
    if audit["subject"] is not None:
        return audit["subject"]
    service = getattr(slots, "service", None)
    if service is not None:
        return service
    names = [str(name) for name in (getattr(slots, "service_names", ()) or ()) if name]
    if len(names) == 1:
        return EntityRef.make(EntityType.SERVICE.value, names[0],
                              getattr(slots, "cluster", None))
    # ★ 多服务但没有可归属的证据 → **不挑一个**（挑一个就是假主语）；
    #   标成 unknown，让"未定论"如实反映在结论里。
    return EntityRef.make(EntityType.SERVICE.value, "unknown",
                          getattr(slots, "cluster", None))
