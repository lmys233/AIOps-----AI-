"""4/6/12. `Evidence` / `EvidenceBoard` / `EvidencePlan`。

★ 本模块的内容原样搬自原 `common/contracts.py`（它曾有 1090 行），
  **公开导入面一字未改** —— `from agent.common.contracts import …` 照旧可用
  （包的 `__init__.py` 显式再导出全部公开名）。
"""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator
from ..enums import EvidenceDirection, EvidenceSource, HypothesisStatus
from typing import Any, Optional
from datetime import datetime
from .base import _Contract
from .entities import EntityRef
from .hypothesis import Hypothesis
from .intent import Slots


# ==================================================================
#  4. Evidence —— 黑板的基本单元
# ==================================================================
class Evidence(_Contract):
    """结构化证据 —— 黑板模式成立的前提。

    **`onset` 是时序因果的基础，必填**；`raw_ref` 必须可回查。
    """

    evidence_id: str = Field(..., min_length=1, description="ULID")
    source: EvidenceSource
    agent_role: str = Field(..., min_length=1)
    entity: EntityRef
    signal: str = Field(..., min_length=1)
    direction: EvidenceDirection

    magnitude: Optional[float] = None
    magnitude_unit: Optional[str] = None
    baseline: Optional[float] = None

    time_range: tuple[datetime, datetime]
    onset: datetime = Field(..., description="★ 变化起始时刻，时序因果分析的基础")

    confidence: float = Field(..., ge=0.0, le=1.0)
    raw_ref: str = Field(..., min_length=1, description="回查链接，必须可回查")
    summary: str = Field(..., min_length=1)
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_time_range(self) -> "Evidence":
        """`onset` 必须落在 `time_range` 内，且区间方向正确。

        ★ 为什么校验：若 onset 落在观测窗口之外，时序因果推断会得出
          错误结论（"变更早于异常"可能只是窗口设错）。
        """
        start, end = self.time_range
        if end < start:
            raise ValueError(f"time_range 的结束时间早于开始时间: {self.time_range}")
        if not (start <= self.onset <= end):
            raise ValueError(
                f"onset {self.onset.isoformat()} 不在 time_range "
                f"[{start.isoformat()}, {end.isoformat()}] 内 —— "
                f"时序因果推断会因此失真"
            )
        return self

    @field_validator("raw_ref")
    @classmethod
    def _check_raw_ref(cls, v: str) -> str:
        """`raw_ref` 必须形如 `promql: ...` / `logql: ...` / `traceql: ...` 等可回查形式。"""
        if ":" not in v:
            raise ValueError(
                f"raw_ref 必须为可回查形式（如 `promql: <query>`），实际 {v!r}"
            )
        return v

# ==================================================================
#  6. EvidenceBoard
# ==================================================================
class EvidenceBoard(_Contract):
    """共享状态黑板 —— Agent 之间**唯一**的通信媒介。

    ★ 早期文档用到但未定义的 7 个字段在此补齐。
    ★ `qps_estimates` 由 Metrics Agent 写入，风险分级消费。
    """

    incident_id: str = Field(..., min_length=1)
    slots: Slots

    timeline: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    attribution_v2: dict[str, Any] = Field(default_factory=dict)

    open_questions: list[str] = Field(
        default_factory=list,
        description="★ 驱动定向二次取证，是『非伪 Agent』的证明",
    )
    round: int = Field(default=1, ge=1)
    budget: dict[str, Any] = Field(default_factory=dict)
    cost: dict[str, Any] = Field(default_factory=dict)

    # ---- 曾经"被文档用到但没定义"的 7 个字段 ----
    memory_hits: list["MemoryHit"] = Field(default_factory=list)
    unused_evidence_ids: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    excluded_hypotheses: list[str] = Field(default_factory=list)
    abnormal_entities: list[str] = Field(default_factory=list)
    last_round_new_evidence_lr_gain: float = 0.0
    baseline_metrics: dict[str, float] = Field(default_factory=dict)

    # ---- QPS 估算（由 Metrics Agent 提供）----
    qps_estimates: dict[str, float] = Field(
        default_factory=dict,
        description="★ 由 Metrics Agent 写入，风险分级消费",
    )

    def confirmed_hypothesis(self) -> Optional[Hypothesis]:
        """返回得分最高且未被否决的假设（收敛判据用）。"""
        candidates = [h for h in self.hypotheses if h.status != HypothesisStatus.EXCLUDED]
        if not candidates:
            return None
        return max(candidates, key=lambda h: h.score)

# ==================================================================
#  12. EvidencePlan
# ==================================================================
class EvidencePlan(_Contract):
    """取证计划。

    必须可解释 ——"查什么、为什么查、查完能排除什么"。
    """

    targets: list[str] = Field(default_factory=list)
    rationale: str = Field(..., min_length=1, description="为什么这样查")
    expected_exclusions: list[str] = Field(
        default_factory=list, description="查完能排除哪些假设"
    )
    signals: list[str] = Field(default_factory=list, description="要取哪几路信号")

    @field_validator("signals")
    @classmethod
    def _check_signals(cls, v: list[str]) -> list[str]:
        allowed = {m.value for m in EvidenceSource}
        bad = [s for s in v if s not in allowed]
        if bad:
            raise ValueError(f"signals 含非契约信号源 {bad}，合法取值: {sorted(allowed)}")
        return v
