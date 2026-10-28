"""编排层：状态图 · 预算终止 · 澄清预算 · 多轮指代消解接线。

--------------------------------------------------------------------------------
★ 这一层解决什么问题
--------------------------------------------------------------------------------

意图识别/最小服务壳与"5 路取证 + 黑板"都已存在，
但它们之间**没有编排**：服务壳拿到意图就出结论，取证只在一 CLI 里跑。
但两者之间**没有编排**：终止条件、检查点恢复、"问几次"的澄清预算都属于这一层。

--------------------------------------------------------------------------------
★ 目录里每个文件负责一件事
--------------------------------------------------------------------------------

| 文件 | 负责 | 关键不变量 |
|---|---|---|
| `graph.py` | 图的**描述**（节点/边/条件边） | 图是唯一控制流；环有界 |
| `state.py` | 工作状态 + `should_terminate()` | 终止判据只有一处实现；`degraded_signals` 是源级闭集 |
| `budget.py` | 五项上限与记账 | 上限只有一个来源（`AgentConfig`）；消耗只记真实发生 |
| `clarify.py` | 澄清预算（`clarify` 的唯一出口） | 确认门不占信息预算；同缺口只问一次 |
| `hypotheses.py` | 假设与打分（**临时**实现） | 分数只用于"要不要再查一轮"；响应里明说是临时的 |
| `checkpoint.py` | 检查点（内存 / 磁盘） | 恢复的是进度，不是伪造的留痕 |
| `orchestrator.py` | 按图驱动一次排查 | `run()` 里没有第二处改状态的地方 |

--------------------------------------------------------------------------------
★ 常用入口
--------------------------------------------------------------------------------

```python
from agent.orchestration import build_graph, Orchestrator

build_graph()                      # 图可编译
await Orchestrator(router=...).run(session_id=..., trace_id=..., message=...)
```
"""

from __future__ import annotations

from .budget import Budget, BudgetExhausted, budget_from_config, DEFAULT_BUDGET
from .checkpoint import (Checkpointer, FileCheckpointer, InMemoryCheckpointer,
                         build_checkpointer, DEFAULT_CHECKPOINT_DIR)
from .clarify import ClarifyBudget, ClarifyDecision, DEFAULT_MAX_QUESTIONS
from .graph import (ENTRY_NODE, MAX_NODE_STEPS, NODE_NAMES, TERMINAL_NODE, Graph,
                    build_graph)
from .hypotheses import (CATEGORY_AGENT_ROLE, CATEGORY_SIGNALS, ProvisionalScorer,
                         SCORING_NOTE, SOURCE_WEIGHT, HypothesisScorer)
from .orchestrator import (CAN_PRODUCE_ROOT_CAUSE, INCIDENT_ID_PREFIX,
                           OrchestrationResult, Orchestrator, new_incident_id)
from .state import (ABSTAIN_BELOW, CONFIRM_SCORE, NO_PROGRESS_GAIN, TOP2_CLOSE,
                    GraphState, TerminationDecision, evidence_gain, should_terminate)

__all__ = [
    # 图
    "Graph", "build_graph", "NODE_NAMES", "ENTRY_NODE", "TERMINAL_NODE", "MAX_NODE_STEPS",
    # 状态与终止
    "GraphState", "TerminationDecision", "should_terminate", "evidence_gain",
    "CONFIRM_SCORE", "NO_PROGRESS_GAIN", "ABSTAIN_BELOW", "TOP2_CLOSE",
    # 预算
    "Budget", "BudgetExhausted", "budget_from_config", "DEFAULT_BUDGET",
    # 澄清
    "ClarifyBudget", "ClarifyDecision", "DEFAULT_MAX_QUESTIONS",
    # 假设与打分
    "HypothesisScorer", "ProvisionalScorer", "SCORING_NOTE",
    "CATEGORY_SIGNALS", "CATEGORY_AGENT_ROLE", "SOURCE_WEIGHT",
    # 检查点
    "Checkpointer", "InMemoryCheckpointer", "FileCheckpointer", "build_checkpointer",
    "DEFAULT_CHECKPOINT_DIR",
    # 编排器
    "Orchestrator", "OrchestrationResult", "INCIDENT_ID_PREFIX", "new_incident_id",
    "CAN_PRODUCE_ROOT_CAUSE",
]
