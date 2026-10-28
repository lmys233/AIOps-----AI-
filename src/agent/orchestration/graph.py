"""编排状态图 —— **图的描述**（节点/边）与运行时装配。

--------------------------------------------------------------------------------
★ 这个"图"是什么、不是什么
--------------------------------------------------------------------------------

它是**显式的控制流描述**：节点名 + 边 + 唯一的那条条件边（`decide`）。
`Orchestrator.run()` **按这份描述走**（不是"另外写一遍 while 循环、图只当注释"）——
这一点很关键："图画在文档里、循环写在代码里"的教训正是两者迟早不一致，
于是两者迟早不一致，而终止条件恰好在图里。

★ 为什么不引 langgraph：本项目零第三方编排依赖（`requirements.txt` 只有
  pydantic / PyYAML / httpx / fastapi / uvicorn）。图只有 8 个节点、1 个环，
  自己写 60 行比引一个框架更好审计 —— 尤其"终止条件必须在关键路径上"这一条，
  自绘的图能直接在单测里断言。

--------------------------------------------------------------------------------
★ 节点与边（顺序即不变量）
--------------------------------------------------------------------------------

```
resolve ──► route ──► clarify ──┬─(要问)──────────────────────────► report
                                └─(不问)──► forensics ──► hypotheses ──► decide
                                                                          │
                                        ┌────────────(定向二次取证)────────┘
                                        ▼
                                    forensics（第 2 轮，只叫相关角色）
                                        │
                                    decide ─(收敛/超预算/无增益/低分)──► report
```

| 节点 | 做什么 | 为什么必须单独成节点 |
|---|---|---|
| `resolve` | 代词消解（用工作记忆） | 必须在**意图层之前**：消解结果要进意图层的输入 |
| `route` | 意图 + 槽位 | 预算里的"模型调用"在这里发生（要记账） |
| `clarify` | 澄清预算裁决（**唯一出口**） | 工具层不得自己反问；问不问是预算问题 |
| `forensics` | 5 路并行取证 | 预算里的"工具调用/轮次"在这里发生 |
| `hypotheses` | 假设与打分（可换真公式） | 三条终止条件依赖它 |
| `decide` | `should_terminate()` | ★ 终止判据**唯一**调用点 |
| `directed` | 把"要查什么"写进 `open_questions` | 定向二次取证的可解释依据（"非伪 Agent"的证明） |
| `report` | 结论 + 不确定性说明 | 报告收口必须是图上的一个节点，不能隐式收尾 |
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = ["Graph", "build_graph", "NODE_NAMES", "ENTRY_NODE", "TERMINAL_NODE",
           "MAX_NODE_STEPS"]

#: 节点名（顺序 = 正常路径上的先后）
NODE_NAMES: Final[tuple[str, ...]] = (
    "resolve", "route", "clarify", "forensics",
    "hypotheses", "decide", "directed", "report",
)

ENTRY_NODE: Final[str] = "resolve"
TERMINAL_NODE: Final[str] = "report"

#: 节点步数上限（**兜底**：图本身有环，万一条件边写错，宁可报错也不要转圈烧钱）
MAX_NODE_STEPS: Final[int] = 32


@dataclass(frozen=True)
class Graph:
    """图描述：节点集合 + 静态边 + 唯一的条件边。"""

    nodes: tuple[str, ...] = NODE_NAMES
    entry: str = ENTRY_NODE
    terminal: str = TERMINAL_NODE
    #: 静态边：`节点 → 下一个节点`（条件边不在这里）
    edges: tuple[tuple[str, str], ...] = (
        ("resolve", "route"),
        ("route", "clarify"),
        ("clarify", "forensics"),
        ("forensics", "hypotheses"),
        ("hypotheses", "decide"),
        ("directed", "forensics"),
        ("report", "report"),               # 终止节点自环（不再前进）
    )
    #: 条件边所在节点（`decide` 的返回值决定去 `directed`、还是去 `report`）
    conditional_node: str = "decide"
    #: 条件边的两个去向：`(继续定向, 收口)`
    conditional_targets: tuple[str, str] = ("directed", TERMINAL_NODE)

    # ------------------------------------------------------------------
    def __contains__(self, node: object) -> bool:
        return node in self.nodes

    def next_of(self, node: str) -> str:
        for source, target in self.edges:
            if source == node:
                return target
        raise KeyError(f"节点 {node!r} 没有出边 —— 图不完整（build_graph 应当拦住）")

    def validate(self) -> "Graph":
        """图的自洽性（**编译期**检查，不用等到运行时才炸）。"""
        edges = self.edges_dict()
        for node in self.nodes:
            if node == self.conditional_node:
                continue          # ★ 条件边节点只有"条件出边"，没有静态出边
            if node not in edges:
                raise ValueError(f"节点 {node!r} 没有出边 —— 图不完整")
        if self.entry not in self.nodes:
            raise ValueError(f"入口节点 {self.entry!r} 不在节点表里")
        if self.terminal not in self.nodes:
            raise ValueError(f"终止节点 {self.terminal!r} 不在节点表里")
        if self.conditional_node not in self.nodes:
            raise ValueError(f"条件边所在节点 {self.conditional_node!r} 不在节点表里")
        for target in self.conditional_targets:
            if target not in self.nodes:
                raise ValueError(f"条件边的去向 {target!r} 不在节点表里")
        if self.terminal != self.next_of(self.terminal):
            raise ValueError(f"终止节点 {self.terminal!r} 必须是自环（跑完就结束）")
        return self

    def edges_dict(self) -> dict[str, str]:
        return dict(self.edges)

    # ------------------------------------------------------------------
    def to_mermaid(self) -> str:
        """给人看的图（文档/排障用；也与单测的断言共享同一份事实）。"""
        lines = ["graph TD", f"  {self.entry}((入口)) --> {self.next_of(self.entry)}"]
        for source, target in self.edges:
            if source == self.entry:
                continue
            if source == self.conditional_node:
                continue
            if source == self.terminal:
                continue
            lines.append(f"  {source} --> {target}")
        lines.append(f"  {self.conditional_node} -.->|收敛/超预算/无增益/低分| {self.terminal}")
        lines.append(f"  {self.conditional_node} -.->|定向二次取证| directed")
        lines.append(f"  directed --> forensics")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        return {"nodes": list(self.nodes), "entry": self.entry, "terminal": self.terminal,
                "edges": [list(edge) for edge in self.edges],
                "conditional_node": self.conditional_node,
                "conditional_targets": list(self.conditional_targets),
                "has_report_node": "report" in self.nodes,
                "mermaid": self.to_mermaid()}


def build_graph() -> Graph:
    """建图（**可编译**）。

    ★ 它不依赖任何运行期对象（router/executor/记忆都可注入到 `Orchestrator`），
      所以"图能不能建起来"是一个纯粹的契约问题 —— 这正是验收项想要的判据。
    """
    return Graph().validate()
