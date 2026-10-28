"""取证 Agent 层 —— 5 路并行取证 + 取证规划 + 运行器。

| 模块 | 职责 |
|---|---|
| `base.py` | 公共骨架：`AgentContext`（**刻意很小**）/ `ToolRequest` / `AgentOutcome` / `ForensicsAgent` |
| `planner.py` | `EvidencePlan` + **可填充性检查**（在发调用之前判"参数填得出来吗"） |
| `anomaly.py` | 「这算不算异常」的唯一判定处（阈值表 + 拐点） |
| `metrics_agent.py` / `log_agent.py` / `trace_agent.py` / `change_agent.py` / `topology_agent.py` | 5 路取证 |
| `runner.py` | 并行执行 + 分类落黑板（"查了没异常" ≠ "没查成"） |

★ 证据隔离是**接口层面的事实**：`AgentContext` 里没有别的 Agent 的输出、
  没有黑板句柄、没有对话历史；每个 Agent 只持有自己的那 1 个工具。
"""

from __future__ import annotations

from .anomaly import METRIC_SIGNALS, Detection, Signal, detect, detect_series
from .base import AGENT_TO_TOOLS, AgentContext, AgentOutcome, ForensicsAgent, ToolRequest
from .change_agent import ChangeAgent
from .log_agent import LogAgent
from .metrics_agent import MetricsAgent
from .planner import (
    FIELD_SOURCES,
    USER_ONLY_FIELDS,
    EvidencePlanner,
    PendingGap,
    PlannedCall,
    PlanOutcome,
)
from .runner import ForensicsRun, ForensicsRunner, default_agents, resolve_window
from .topology_agent import TopologyAgent
from .trace_agent import TraceAgent

__all__ = [
    # 骨架
    "AGENT_TO_TOOLS",
    "AgentContext",
    "AgentOutcome",
    "ForensicsAgent",
    "ToolRequest",
    # 规划
    "EvidencePlanner",
    "PlanOutcome",
    "PlannedCall",
    "PendingGap",
    "FIELD_SOURCES",
    "USER_ONLY_FIELDS",
    # 判定
    "Signal",
    "Detection",
    "METRIC_SIGNALS",
    "detect",
    "detect_series",
    # 5 路取证
    "MetricsAgent",
    "LogAgent",
    "TraceAgent",
    "ChangeAgent",
    "TopologyAgent",
    # 运行
    "ForensicsRunner",
    "ForensicsRun",
    "default_agents",
    "resolve_window",
]
