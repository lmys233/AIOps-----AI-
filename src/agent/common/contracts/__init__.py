"""全局数据契约。

★ 这 16 个对象是"项目的语言"。后续所有组件都基于它们通信。

设计原则：

1. **必须机器可读**，不是自然语言 —— 这是黑板模式成立、可审计、可单独评测的前提。
2. **校验在模型层完成**，不留给调用方。凡是定了约束的（枚举取值、必填、
   跨字段一致性），都在这里用 `field_validator` / `model_validator` 强制。
3. **★ 区分"查了没异常"与"没查成"** ——
   `ToolResult` 的两个字段必须严格对应，这是本项目最容易翻车的地方。
4. **枚举取值一律来自 `enums` 模块**，禁止在本文件重新写字面量。

关于 `ALL_CONTRACTS`：按固定顺序列出全部对象，
供验收脚本与实现侧对照使用。"""


# ----------------------------------------------------------------------
#  本包由原先的 `contracts.py`（1090 行）拆分而来。
#  ★ 下面这些再导出**就是对外契约**：`from agent.common.contracts import EntityRef`
#    之类的写法一个字都不用改（拆文件不该让调用方改代码）。
# ----------------------------------------------------------------------
from .agent_state import AgentState
from .base import (ENTITY_ID_PREFIX, EnvName, EntityType, PLACEHOLDER_CLUSTERS,
                   _Contract)
from .entities import EntityRef
from .evidence import Evidence, EvidenceBoard, EvidencePlan
from .hypothesis import ArbitResult, Hypothesis
from .intent import IntentResult, Slots
from .memory import MemoryHit
from .samples import ALL_CONTRACTS, build_contract_samples
from .tools import (ChangeResult, ProposedAction, RiskDecision, ToolResult, ToolSpec,
                    WriteResult)

# ★ 前向引用解析（`EvidenceBoard` ↔ `MemoryHit` / `AgentState` / `ArbitResult` 互相引用）。
#   原文件在模块末尾调用；现在按"所有模型都定义完"之后在这里统一调一次。
EvidenceBoard.model_rebuild()
AgentState.model_rebuild()
ArbitResult.model_rebuild()

__all__ = [
    # ---- 16 个契约对象 ----
    "EntityRef", "Slots", "IntentResult", "Evidence", "Hypothesis", "EvidenceBoard",
    "MemoryHit", "AgentState", "ToolResult", "ArbitResult", "WriteResult",
    "EvidencePlan", "ChangeResult", "ToolSpec", "ProposedAction", "RiskDecision",
    # ---- 辅助 ----
    "ALL_CONTRACTS", "build_contract_samples", "PLACEHOLDER_CLUSTERS",
    # ---- 契约自带的枚举/常量（`EntityType` / `EnvName` 只服务本文件的取值约束）----
    "EntityType", "EnvName", "ENTITY_ID_PREFIX",
]
