"""8. `AgentState`（一次排查的完整状态）。

★ 本模块的内容原样搬自原 `common/contracts.py`（它曾有 1090 行），
  **公开导入面一字未改** —— `from agent.common.contracts import …` 照旧可用
  （包的 `__init__.py` 显式再导出全部公开名）。
"""

from __future__ import annotations

from ..enums import EvidenceSource
from typing import Any, Optional
from pydantic import Field, field_validator
from .base import _Contract
from .evidence import EvidenceBoard
from .intent import Slots


# ==================================================================
#  8. AgentState
# ==================================================================
class AgentState(_Contract):
    """状态图的**唯一状态契约**（不定义则图无法编译）。"""

    board: EvidenceBoard
    slots: Slots
    intent: Optional[str] = None
    round: int = Field(default=1, ge=1)
    budget: dict[str, Any] = Field(default_factory=dict)
    cost: dict[str, Any] = Field(default_factory=dict)

    proposed_action: Optional["ProposedAction"] = None
    approval: Optional[dict[str, Any]] = None
    status: str = "running"
    trace_id: str = Field(..., min_length=1)
    degraded_signals: list[str] = Field(
        default_factory=list,
        description="部分失败时缺失的信号源，必须对用户可见",
    )

    @field_validator("degraded_signals")
    @classmethod
    def _check_degraded(cls, v: list[str]) -> list[str]:
        """缺失信号必须来自契约内的数据源，否则无法判断"少了哪一路"。"""
        allowed = {m.value for m in EvidenceSource}
        bad = [s for s in v if s not in allowed]
        if bad:
            raise ValueError(
                f"degraded_signals 含非契约数据源 {bad}，"
                f"合法取值: {sorted(allowed)}"
            )
        return v
