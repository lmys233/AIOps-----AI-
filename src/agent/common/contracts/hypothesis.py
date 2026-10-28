"""5/10. `Hypothesis` / `ArbitResult`。

★ 本模块的内容原样搬自原 `common/contracts.py`（它曾有 1090 行），
  **公开导入面一字未改** —— `from agent.common.contracts import …` 照旧可用
  （包的 `__init__.py` 显式再导出全部公开名）。
"""

from __future__ import annotations

from pydantic import Field, model_validator
from ..enums import ArbitDecision, HypothesisStatus
from typing import Optional
from .base import _Contract
from .entities import EntityRef


# ==================================================================
#  5. Hypothesis
# ==================================================================
class Hypothesis(_Contract):
    """候选假设及其证据加权得分。"""

    hypothesis_id: str = Field(..., min_length=1)
    statement: str = Field(..., min_length=1)
    category: str = Field(..., min_length=1)
    root_entity: EntityRef

    prior: float = Field(..., ge=0.0, le=1.0, description="先验（加权平均，不饱和）")
    evidence_likelihoods: dict[str, float] = Field(
        default_factory=dict, description="evidence_id → 似然比 LR"
    )
    score: float = Field(..., ge=0.0, le=1.0)

    supporting: list[str] = Field(default_factory=list)
    contradicting: list[str] = Field(default_factory=list)
    necessary_evidence: list[str] = Field(default_factory=list)
    missing_necessary: list[str] = Field(default_factory=list)

    status: HypothesisStatus = HypothesisStatus.CANDIDATE
    excluded_reason: Optional[str] = None

    @model_validator(mode="after")
    def _check_status_semantics(self) -> "Hypothesis":
        """状态与证据引用的语义一致性。

        · `confirmed` 必须有支持证据 —— 结论必须可追溯到证据。
        · `excluded` 必须给排除理由 —— 被否决要记录理由，
          否则"为何排除"无法复核。
        """
        if self.status == HypothesisStatus.CONFIRMED and not self.supporting:
            raise ValueError(
                "status=confirmed 却无 supporting 证据 —— "
                "结论必须可追溯到证据"
            )
        if self.status == HypothesisStatus.EXCLUDED and not self.excluded_reason:
            raise ValueError(
                "status=excluded 但未给 excluded_reason —— "
                "被否决的假设必须记录理由"
            )
        return self

# ==================================================================
#  10. ArbitResult
# ==================================================================
class ArbitResult(_Contract):
    """仲裁结果。"""

    decision: ArbitDecision
    root_cause: Optional[Hypothesis] = None
    #: ★★ 这是**按证据强度折算的校准分**，不是候选间的 softmax 份额、
    #: 也不是概率 —— `score`（份额，和为 1）回答"谁排在前面"，`confidence` 回答
    #: "按现有证据敢下多大决心"。口径见 `attribution/arbiter._calibrated_confidence`，
    #: 上界 `CONFIDENCE_CEILING=0.95`（**不允许 1.0**：任何从证据算出来的分数都
    #: 不该声称"确定"）。
    confidence: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description=("校准分（证据强度折算），**不是** softmax 份额、也不是概率；"
                     "上界 0.95"))
    alternatives: list[Hypothesis] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    report: Optional[str] = None
    reason: Optional[str] = None

    @model_validator(mode="after")
    def _check_decision_payload(self) -> "ArbitResult":
        """各决策分支必须带齐该分支专属的载荷。

        · `abstain`（拒答）→ 必须给 report：输出『已收集证据 + 未排除可能 +
          建议人工方向』。★ 拒答是**正确行为**，但必须给出可行动的说明，
          不能是空响应。
        · `need_targeted_evidence` → 必须给 open_questions，
          否则无法驱动第二轮定向取证。
        """
        if self.decision == ArbitDecision.ABSTAIN and not self.report:
            raise ValueError(
                "decision=abstain 但无 report —— 拒答必须输出"
                "『已收集证据 + 未排除可能 + 建议人工方向』"
            )
        if self.decision == ArbitDecision.NEED_TARGETED_EVIDENCE and not self.open_questions:
            raise ValueError(
                "decision=need_targeted_evidence 但无 open_questions —— "
                "无法驱动定向二次取证"
            )
        return self
