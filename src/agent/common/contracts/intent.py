"""2~3. `Slots` / `IntentResult`（意图识别的输入与产出）。

★ 本模块的内容原样搬自原 `common/contracts.py`（它曾有 1090 行），
  **公开导入面一字未改** —— `from agent.common.contracts import …` 照旧可用
  （包的 `__init__.py` 显式再导出全部公开名）。
"""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator
from ..enums import Intent, IntentSource
from typing import Any, Optional
from .base import EnvName, _Contract
from .entities import EntityRef


# ==================================================================
#  2. Slots
# ==================================================================
class Slots(_Contract):
    observed_id: Optional[str] = Field(default=None,pattern=r'^obs_[a-f0-9]{32}$')
    """意图识别的槽位。

    ★ 必须补齐 `namespace` / `service_names` / `tenant_id` ——
      早期文档的伪代码用到这三个字段却没定义，会导致 `AttributeError`。
    """

    service: Optional[EntityRef] = None
    env: EnvName = EnvName.PROD
    cluster: Optional[str] = None
    time_window: str = "30m"

    symptom: Optional[str] = None
    change_id: Optional[str] = None
    slo_target: Optional[str] = None

    # ---- 曾经"被伪代码用到但没定义"的三个字段 ----
    namespace: Optional[str] = None
    service_names: list[str] = Field(default_factory=list)
    tenant_id: str = "default"

    @field_validator("time_window")
    @classmethod
    def _check_time_window(cls, v: str) -> str:
        """`time_window` 为规范化时长字符串（如 `30m`）。"""
        import re
        if not re.fullmatch(r"\d+(s|m|h|d)", str(v)):
            raise ValueError(
                f"time_window 必须是规范化时长字符串（如 30m / 2h / 1d），实际 {v!r}"
            )
        return v

    @field_validator("tenant_id")
    @classmethod
    def _check_single_tenant(cls, v: str) -> str:
        """单租户阶段 `tenant_id` 固定为 `"default"`（多租户预留）。"""
        if v != "default":
            raise ValueError(
                f"单租户阶段 tenant_id 必须为 'default'（多租户为预留能力），实际 {v!r}"
            )
        return v

# ==================================================================
#  3. IntentResult
# ==================================================================
class IntentResult(_Contract):
    """意图识别结果。

    `confidence` **必须是校准后**的值；
    另有 `confidence_raw` 保留模型原始输出，用于校准分析。
    """

    intent: str
    confidence: float = Field(..., ge=0.0, le=1.0, description="校准后的置信度")
    confidence_raw: float = Field(..., ge=0.0, le=1.0, description="模型原始置信度")
    slots: Slots
    unresolved_entities: list[str] = Field(default_factory=list)
    clarification: Optional[dict[str, Any]] = None
    source: IntentSource
    route: str = Field(..., min_length=1)
    latency_ms: int = Field(..., ge=0)

    @field_validator("intent")
    @classmethod
    def _check_intent(cls, v: str) -> str:
        """意图必须落在 6 类体系内（MECE，含兜底类）。"""
        allowed = {m.value for m in Intent}
        if v not in allowed:
            raise ValueError(f"intent {v!r} 不在 6 类意图体系内: {sorted(allowed)}")
        return v

    @model_validator(mode="after")
    def _check_clarification(self) -> "IntentResult":
        """若需要澄清，`clarification` 必须给出可展示的问题列表。

        `status=clarify` 时客户端要展示 `clarification.questions`，
        没有 questions 客户端无从追问。
        """
        if self.clarification is not None:
            qs = self.clarification.get("questions")
            if not isinstance(qs, list) or not qs:
                raise ValueError(
                    "clarification 非空时必须含非空的 questions 列表"
                    "（客户端要展示 clarification.questions）"
                )
        return self
