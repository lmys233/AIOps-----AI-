"""编排预算 —— 五项上限 + 记账。

--------------------------------------------------------------------------------
★ 为什么预算必须是"一个对象"，而不是散在各处的计数器
--------------------------------------------------------------------------------

五个终止条件里，预算是**唯一一个"必然会到"的条件**：
轮次/LLM/工具/Token/墙钟，任何一次真实排查迟早会撞上其中一条。
如果每条上限分别写在它管的地方（轮次在编排循环里、token 在模型调用处、
工具次数在执行器里……），那么"到底有没有超预算"就没人能回答 ——
这正是"上限散落"那类缺陷：终止函数定义了、但没人能回答"到底超没超"。

所以这里定成一条：**上限与消耗都在这一个对象里**，`exhausted()` 是唯一判据。

--------------------------------------------------------------------------------
★ 上限从哪来：`AgentConfig`，不重新读环境变量
--------------------------------------------------------------------------------

`common/config.py` 已经从 `AIOPS_MAX_*` 装载了这五项（默认值就是这五项标准值：
3 / 40 / 80 / 200000 / 300），所以编排层**不再读一遍环境变量** ——
同一事实只留一份（重复定义已经引发过三次故障）。

--------------------------------------------------------------------------------
★ 记账的粒度：**真实发生**，不是声明
--------------------------------------------------------------------------------

`rounds` 只在本轮真的跑过取证时 +1；`llm_calls` 来自路由留痕里**实际发生的**
模型调用条数；`tokens` 来自 LLM 网关的用量回调；`tool_calls` 来自取证 Agent
`AgentOutcome.calls` 的长度。★ 与 `options` 那条修复同一条口径：
**不许把"请求里声明的上限"当成"已经发生的消耗"**（`enforced` 那种假账）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Optional

__all__ = ["Budget", "BudgetExhausted", "budget_from_config", "DEFAULT_BUDGET"]

#: 默认值（仅用于"没有配置对象"的单测与文档；生产一律走 `AgentConfig`）
DEFAULT_BUDGET: Final[dict[str, int]] = {
    "max_rounds": 3,
    "max_llm_calls": 40,
    "max_tool_calls": 80,
    "max_tokens": 200_000,
    "max_wall_time_sec": 300,
}


class BudgetExhausted(Exception):
    """预算耗尽（图用它把"降级输出"和"实现 bug"分开）。

    ★ 它不是错误："预算耗尽 → 降级输出最佳候选 + 不确定性说明"。
      单独一个异常类型，是为了让上层的 `except` 不至于把"超预算"和"代码炸了"混在一起。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Budget:
    """五项上限 + 消耗记账。**`exhausted()` 是唯一判据。**"""

    max_rounds: int = DEFAULT_BUDGET["max_rounds"]
    max_llm_calls: int = DEFAULT_BUDGET["max_llm_calls"]
    max_tool_calls: int = DEFAULT_BUDGET["max_tool_calls"]
    max_tokens: int = DEFAULT_BUDGET["max_tokens"]
    max_wall_time_sec: int = DEFAULT_BUDGET["max_wall_time_sec"]

    # ---- 消耗（只能通过下面的 `spend_*` 改）----
    rounds: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    tokens: int = 0
    reserved_tokens: int = 0
    elapsed_sec: float = 0.0

    #: 人工中止（"人工中止 → 立即停止"）—— 由调用方/恢复接口置位
    aborted: bool = False
    #: 触发中止/耗尽的细节（给人看；也进响应）
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    @classmethod
    def from_agent_config(cls, cfg: Any) -> "Budget":
        """从 `AgentConfig` 建预算（**唯一来源**；缺字段就如实报错，不猜默认值）。"""
        missing = [name for name in
                   ("max_rounds", "max_llm_calls", "max_tool_calls",
                    "max_tokens", "max_wall_time_sec")
                   if getattr(cfg, name, None) is None]
        if missing:
            raise ValueError(
                f"AgentConfig 缺预算字段 {missing} —— 预算必须来自配置，"
                f"编排层不自行发明默认值")
        return cls(max_rounds=int(cfg.max_rounds), max_llm_calls=int(cfg.max_llm_calls),
                   max_tool_calls=int(cfg.max_tool_calls), max_tokens=int(cfg.max_tokens),
                   max_wall_time_sec=int(cfg.max_wall_time_sec))

    # ------------------------------------------------------------------
    #  记账
    # ------------------------------------------------------------------
    def spend_round(self, n: int = 1) -> None:
        self.rounds += max(0, int(n))

    def spend_llm(self, calls: int = 0, tokens: int = 0) -> None:
        self.llm_calls += max(0, int(calls))
        self.tokens += max(0, int(tokens))

    def spend_tool(self, n: int = 1) -> None:
        self.tool_calls += max(0, int(n))

    def observe_elapsed(self, seconds: float) -> None:
        self.elapsed_sec = max(self.elapsed_sec, float(seconds))

    def abort(self, reason: str) -> None:
        self.aborted = True
        self.notes.append(str(reason))

    # ------------------------------------------------------------------
    #  判据
    # ------------------------------------------------------------------
    def exhausted(self) -> Optional[str]:
        """返回**耗尽原因**（`None` = 还有余量）。

        ★ 顺序即优先级：人工中止 > 墙钟 > 轮次 > LLM > 工具 > Token。
          墙钟排在最前，因为它是唯一"不看进度、只看时间"的那条 ——
          一次排查卡在某个网络等待里时，只有它能把请求救回来。
        """
        if self.aborted:
            return f"人工中止（{self.notes[-1] if self.notes else '未说明原因'}）"
        if self.elapsed_sec >= self.max_wall_time_sec:
            return (f"墙钟 {self.elapsed_sec:.1f}s ≥ {self.max_wall_time_sec}s"
                    f"（预算上限）")
        if self.rounds >= self.max_rounds:
            return f"轮次 {self.rounds}/{self.max_rounds}（预算上限）"
        if self.llm_calls >= self.max_llm_calls:
            return f"模型调用 {self.llm_calls}/{self.max_llm_calls}（预算上限）"
        if self.tool_calls >= self.max_tool_calls:
            return f"工具调用 {self.tool_calls}/{self.max_tool_calls}（预算上限）"
        if self.tokens+self.reserved_tokens >= self.max_tokens:
            if self.reserved_tokens:
                return f"Token 已报告 {self.tokens} + 预留 {self.reserved_tokens} ≥ {self.max_tokens}（预算上限；预留不是账单）"
            return f"Token {self.tokens}/{self.max_tokens}（预算上限）"
        return None

    def has_round_left(self) -> bool:
        """还能不能再开一轮取证（轮次是"开新轮"的门，其余是"继续跑"的门）。"""
        return not self.aborted and self.rounds < self.max_rounds

    # ------------------------------------------------------------------
    def limits(self) -> dict[str, Any]:
        """进 `AgentState.budget`（**上限**，不是消耗）。"""
        return {
            "max_rounds": self.max_rounds,
            "max_llm_calls": self.max_llm_calls,
            "max_tool_calls": self.max_tool_calls,
            "max_tokens": self.max_tokens,
            "max_wall_time_sec": self.max_wall_time_sec,
        }

    def usage(self) -> dict[str, Any]:
        """进 `AgentState.cost`（**真实发生的消耗**）。"""
        return {
            "rounds": self.rounds,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "tokens": self.tokens,
            "reserved_tokens": self.reserved_tokens,
            "wall_time_sec": round(self.elapsed_sec, 3),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.limits(), **self.usage(),
                "exhausted": self.exhausted(), "notes": list(self.notes)}


def budget_from_config(cfg: Any = None) -> Budget:
    """有配置就用配置；没有就用内置默认值（单测/离线）。"""
    return Budget.from_agent_config(cfg) if cfg is not None else Budget()
