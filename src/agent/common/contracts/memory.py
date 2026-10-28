"""7. `MemoryHit`（历史相似案例）。

★ 本模块的内容原样搬自原 `common/contracts.py`（它曾有 1090 行），
  **公开导入面一字未改** —— `from agent.common.contracts import …` 照旧可用
  （包的 `__init__.py` 显式再导出全部公开名）。
"""

from __future__ import annotations

from typing import Optional
from pydantic import Field
from ..enums import VERIFY_WEIGHTS, VerificationMethod
from .base import _Contract


# ==================================================================
#  7. MemoryHit
# ==================================================================
class MemoryHit(_Contract):
    """记忆检索命中项。

    注入上下文时**必须带元数据**（时间/验证方式/命中次数/是否被取代），
    让模型自行判断可信度，而不是把旧结论当既定事实。
    """

    memory_id: str = Field(..., min_length=1)
    symptom_text: str = Field(..., min_length=1)
    root_cause: str = Field(..., min_length=1)
    similarity: float = Field(..., ge=0.0, le=1.0)
    age_days: float = Field(..., ge=0.0)
    verification: VerificationMethod
    hit_count: int = Field(default=0, ge=0)
    superseded_by: Optional[str] = None

    @property
    def verify_weight(self) -> float:
        """该记忆的验证强度权重（由验证方式查表得到）。"""
        return VERIFY_WEIGHTS[self.verification.value]
