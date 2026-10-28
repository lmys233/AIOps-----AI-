"""编排层用的**临时**打分器（默认实现可换成 `attribution.RcaAgent`）。

★★ 名字里必须写"临时"：它给不出校准过的分数，只回答"证据覆盖了哪几类"。
   它仍然保留，用途是**消融对照**（对照矩阵里
   有 `no_critic` 这类组，也需要一个"只会数证据份额"的基线）。

类别词表与角色映射来自 `attribution/categories.py`（归因层与编排层共用同一份）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Protocol, Sequence

from ..attribution.categories import (CATEGORY_AGENT_ROLE, CATEGORY_LABEL,
                                      CATEGORY_SIGNALS)
from ..common.contracts import EntityRef, EntityType, Evidence, Hypothesis
from ..common.enums import EvidenceSource, HypothesisStatus

__all__ = [
    "HypothesisScorer",
    "ProvisionalScorer",
    "CATEGORY_SIGNALS",
    "CATEGORY_LABEL",
    "CATEGORY_AGENT_ROLE",
    "SOURCE_WEIGHT",
    "MIN_KEEP",
    "SCORING_NOTE",
]

#: 打分口径的**人话说明**（进响应的 `explanation.scoring`）
SCORING_NOTE: Final[str] = (
    "provisional(T19)：按「证据覆盖的类别份额」排序 —— 不是 D1/D2 的加权先验 + "
    "对数空间 softmax（T20 起默认实现换成 attribution.RcaAgent）；"
    "本实现保留用于消融对照"
)

#: 证据源的权威性权重（★ 临时值：真公式给出的是**先验**权重，不是这个）
#: 口径：**结构化且直接**的证据（变更记录、指标）> 文本聚类 > 拓扑推断。
SOURCE_WEIGHT: Final[dict[str, float]] = {
    EvidenceSource.CHANGES.value: 1.0,
    EvidenceSource.METRICS.value: 0.9,
    EvidenceSource.TRACES.value: 0.8,
    EvidenceSource.LOGS.value: 0.7,
    EvidenceSource.TOPOLOGY.value: 0.5,
    EvidenceSource.MEMORY.value: 0.4,
    EvidenceSource.HUMAN.value: 0.6,
}

#: 低于这个份额的候选不发给用户（避免 8 个类别全上）
MIN_KEEP: Final[float] = 0.05


class HypothesisScorer(Protocol):
    """打分器协议：**输入证据、输出假设**。

    ★ 协议定在这里、实现可换（`ProvisionalScorer` → `attribution.RcaAgent`），
      编排层的注入点与测试替身都不用改 —— 这就是当初把它做成协议的目的。
    """

    def score(self, *, evidence: Sequence[Evidence], slots: Any,
              round_no: int = 1) -> list[Hypothesis]: ...


@dataclass
class ProvisionalScorer:
    """临时打分器（见模块头）。**可注入，方便测试直接构造 top-2 接近的场景。**"""

    min_keep: float = MIN_KEEP

    def score(self, *, evidence: Sequence[Evidence], slots: Any,
              round_no: int = 1) -> list[Hypothesis]:
        entity = _root_entity(slots)
        weights: dict[str, float] = {}
        supporting: dict[str, list[str]] = {}
        for item in evidence:
            signal = str(item.signal or "")
            for category, keywords in CATEGORY_SIGNALS.items():
                if not any(keyword in signal for keyword in keywords):
                    continue
                weights[category] = weights.get(category, 0.0) + \
                    SOURCE_WEIGHT.get(item.source.value, 0.5)
                supporting.setdefault(category, []).append(item.evidence_id)

        total = sum(weights.values())
        if total <= 0:
            return []

        out: list[Hypothesis] = []
        for index, (category, weight) in enumerate(
                sorted(weights.items(), key=lambda pair: (-pair[1], pair[0]))):
            share = round(weight / total, 4)
            if share < self.min_keep:
                continue
            out.append(Hypothesis(
                hypothesis_id=f"H{index + 1}-{category}",
                statement=(f"{CATEGORY_LABEL.get(category, category)}："
                           f"证据集中在 {len(supporting[category])} 条"
                           f"（{', '.join(sorted(set(supporting[category]))[:4])}）"),
                category=category,
                root_entity=entity,
                # ★ 均匀先验（临时）：真公式要求的是加权平均先验
                prior=round(1.0 / max(1, len(weights)), 4),
                score=share,
                supporting=sorted(set(supporting[category])),
                status=HypothesisStatus.CANDIDATE,
            ))
        return out


def _root_entity(slots: Any) -> EntityRef:
    """假设的根实体：优先槽位里抽到的服务，其次一个**明确标注**的占位实体。

    ★ 不发明服务名：没有服务时用 `unknown` 作为 name（`make` 会原样保留），
      同时把 `type` 标成 `service` —— 这样下游一眼能看出"这条假设没有落到具体服务"。
    """
    service = getattr(slots, "service", None)
    if service is not None:
        return service
    names = list(getattr(slots, "service_names", ()) or ())
    if names:
        return EntityRef.make(EntityType.SERVICE.value, names[0],
                              getattr(slots, "cluster", None))
    return EntityRef.make(EntityType.SERVICE.value, "unknown",
                          getattr(slots, "cluster", None))
