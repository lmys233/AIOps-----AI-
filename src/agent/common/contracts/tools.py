"""9/11/13/14/15/16. 工具与变更相关的六个对象。

★ 本模块的内容原样搬自原 `common/contracts.py`（它曾有 1090 行），
  **公开导入面一字未改** —— `from agent.common.contracts import …` 照旧可用
  （包的 `__init__.py` 显式再导出全部公开名）。
"""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator
from ..enums import (
    DEFAULT_ACTION_LEVEL,
    ERROR_CODE_SET,
    ERROR_RETRY_POLICY,
    ActionClass,
    ChangeStage,
    ErrorCode,
    RiskLevel,
    ToolPermission,
)
from typing import Any, Optional
from datetime import datetime
from .base import _Contract
from .entities import EntityRef


# ==================================================================
#  9. ToolResult
# ==================================================================
class ToolResult(_Contract):
    """工具调用结果。

    ★★ 本对象承载本项目**最容易翻车**的一条语义：

        「正常但无数据」→ `degraded=False, data=[]`  —— 语义为"查了，没异常"
        「数据源不可用」→ `degraded=True,  data=None` —— 语义为"没查成"

      若两者混淆：Loki 挂掉返回空集时，RCA 会误以为"没有错误日志"
      从而**排除掉正确假设**。因此这里用模型校验强制二者严格对应。
    """

    ok: bool
    data: Optional[Any] = None
    error_code: Optional[str] = None
    error_type: Optional[str] = None
    degraded: bool = False
    summary: str = ""
    raw_ref: str = ""
    elapsed_ms: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def _check_semantics(self) -> "ToolResult":
        # 1) "没查成" ⟺ data is None
        if self.degraded and self.data is not None:
            raise ValueError(
                "degraded=True 表示『没查成』，此时 data 必须为 None"
            )
        # 2) "查了没异常" 必须返回空列表而非 None
        if not self.degraded and self.ok and self.data is None:
            raise ValueError(
                "degraded=False 且 ok=True 时 data 不得为 None —— "
                "『查了没异常』必须返回 data=[]，否则与『没查成』无法区分"
            )
        # 3) 失败必须给错误码
        if not self.ok and not self.error_code:
            raise ValueError("ok=False 时必须提供 error_code")
        # 4) 错误码必须在契约内
        if self.error_code is not None and self.error_code not in ERROR_CODE_SET:
            raise ValueError(
                f"error_code {self.error_code!r} 不是已定义的错误码"
            )
        return self

    @property
    def retryable(self) -> bool:
        """该结果对应的错误是否可重试（按错误码查处置矩阵）。"""
        if not self.error_code:
            return False
        return bool(ERROR_RETRY_POLICY.get(self.error_code, {}).get("retryable"))

    @classmethod
    def empty_ok(cls, summary: str = "查了，无异常", raw_ref: str = "") -> "ToolResult":
        """构造"查了没异常"的标准结果（避免各处手写导致语义漂移）。"""
        return cls(ok=True, data=[], degraded=False, summary=summary, raw_ref=raw_ref)

    @classmethod
    def unavailable(cls, error_type: str = "ConnectionError",
                    summary: str = "数据源不可用，没查成",
                    *, raw_ref: str = "", elapsed_ms: float = 0.0) -> "ToolResult":
        """构造"没查成"的标准结果（degraded=True, data=None）。

        ★ `raw_ref` 在"没查成"这条路径上**尤其重要**：它是**唯一**能让人事后
          手工重跑那次查询的线索（"当时到底查了什么？"）。
          少了它，排障只能靠猜 —— 所以这里显式收这个参数，而不是让它空着。
        """
        return cls(
            ok=False,
            data=None,
            error_code=ErrorCode.TOOL_UNAVAILABLE.value,
            error_type=error_type,
            degraded=True,
            summary=summary,
            raw_ref=raw_ref,
            elapsed_ms=elapsed_ms,
        )

# ==================================================================
#  11. WriteResult
# ==================================================================
class WriteResult(_Contract):
    """记忆写入结果。

    被闸门拦下时 `skipped=True`，**不得抛异常**。
    ★ 这一点很重要 —— 闸门拦截是**正常业务流**（例如未验证的结论不该写长期记忆），
      不是错误，所以必须用返回值表达而非异常。
    """

    memory_id: Optional[str] = None
    batch_id: Optional[str] = None
    skipped: bool = False
    reason: Optional[str] = None

    @model_validator(mode="after")
    def _check_semantics(self) -> "WriteResult":
        if self.skipped and not self.reason:
            raise ValueError(
                "skipped=True 但无 reason —— "
                "被写入闸门拦下时必须说明原因"
            )
        if not self.skipped and not self.memory_id:
            raise ValueError("skipped=False 但无 memory_id —— 写入成功应返回 id")
        return self

# ==================================================================
#  13. ChangeResult
# ==================================================================
class ChangeResult(_Contract):
    """变更执行结果（对应变更执行 5 步）。"""

    success: bool
    stage: ChangeStage
    commit: Optional[str] = None
    recovered_at: Optional[datetime] = None
    error: Optional[str] = None
    rolled_back: bool = False

    @model_validator(mode="after")
    def _check_semantics(self) -> "ChangeResult":
        if not self.success and not self.error:
            raise ValueError("success=False 但未提供 error 说明失败原因")
        if self.stage == ChangeStage.VERIFIED and not self.success:
            raise ValueError("stage=verified 表示已验证通过，success 不应为 False")
        return self

# ==================================================================
#  14. ToolSpec
# ==================================================================
class ToolSpec(_Contract):
    """工具声明（每个工具都必须声明自己）。"""

    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1, description="给 LLM 看，决定它会不会用对")
    input_schema: dict[str, Any] = Field(..., description="JSON Schema，供第 1 道保护使用")
    permission: ToolPermission
    timeout_sec: int = Field(default=30, gt=0)
    rate_limit: str = Field(default="60/min", min_length=1)
    cost_weight: float = Field(default=1.0, ge=0.0)
    idempotent: bool = True
    result_summarizer: str = Field(
        ..., min_length=1,
        description="★ 结果裁剪函数名。缺失会让大量返回值撑爆上下文",
    )

    @field_validator("input_schema")
    @classmethod
    def _check_schema(cls, v: dict[str, Any]) -> dict[str, Any]:
        """input_schema 必须是非空 JSON Schema（第 1 道保护依赖它）。"""
        if not v:
            raise ValueError(
                "input_schema 不能为空 —— 第 1 道保护（Schema 校验）依赖它"
            )
        if "type" not in v and "properties" not in v and "$ref" not in v:
            raise ValueError(
                "input_schema 看起来不是 JSON Schema（缺少 type/properties/$ref）"
            )
        return v

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        """工具名不得绑定具体存储实现。

        例：用 `topology_read` 而非 `cypher_read` —— 换存储时 Agent 侧应零改动。
        """
        banned = ("cypher", "milvus", "opensearch", "elasticsearch", "neo4j")
        low = v.lower()
        hit = [b for b in banned if b in low]
        if hit:
            raise ValueError(
                f"工具名 {v!r} 绑定了具体存储实现 {hit} —— "
                f"工具名必须与存储实现解耦"
            )
        return v

# ==================================================================
#  15. ProposedAction
# ==================================================================
class ProposedAction(_Contract):
    """提议的变更动作。"""

    action_class: ActionClass
    target: EntityRef
    params: dict[str, Any] = Field(default_factory=dict)
    dry_run_diff: Optional[str] = None
    rollback_plan: Optional[str] = None

    @property
    def default_level(self) -> str:
        """该动作的**起点风险等级**（查 DEFAULT_ACTION_LEVEL）。

        ★ 每个 action_class 都必然能取到值 —— 由 ActionClass 枚举
          与映射表的穷尽性断言保证（防止"漏一个就走空分支"的回归）。
        """
        return DEFAULT_ACTION_LEVEL[self.action_class.value]

# ==================================================================
#  16. RiskDecision
# ==================================================================
class RiskDecision(_Contract):
    """风险分级判定结果。"""

    level: RiskLevel
    reasons: list[str] = Field(default_factory=list)
    blast_radius: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = False
    auto_rollback_window_sec: int = Field(default=180, ge=0)

    @model_validator(mode="after")
    def _check_level_semantics(self) -> "RiskDecision":
        """等级与审批需求必须自洽。

        · L0/L1：自动，无需审批
        · L2：自动执行 + 事后审计，无需审批
        · L3：**强制人工审批**
        · L4：**直接拒绝**，不进入审批流程
        """
        if self.level == RiskLevel.L3 and not self.requires_approval:
            raise ValueError(
                "level=L3 但 requires_approval≠True —— "
                "L3 高危必须强制人工审批"
            )
        if self.level == RiskLevel.L4 and self.requires_approval:
            raise ValueError(
                "level=L4 却 requires_approval=True —— "
                "L4 是直接拒绝，不进入审批流程"
            )
        if self.level in (RiskLevel.L0, RiskLevel.L1, RiskLevel.L2) \
                and self.requires_approval:
            raise ValueError(
                f"level={self.level.value} 不应需要审批 —— "
                f"L0/L1/L2 均为自动执行"
            )
        return self
