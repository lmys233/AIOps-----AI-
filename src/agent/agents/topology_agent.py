"""Topology Agent —— 依赖图 + 上下游 + **爆炸半径**。

--------------------------------------------------------------------------------
★ 拓扑证据是"结构性事实"，它的方向是 `FLAT`
--------------------------------------------------------------------------------

`Evidence.direction` 描述的是"这个量在涨还是在跌"。依赖关系**不涨也不跌** ——
它是这次排查期间**一直成立的结构**。所以这一路用 `FLAT`，并在 `onset` 上
如实取观测窗口起点（"结构事实在窗口内始终成立"），**不编造一个拐点**。

★ 两个必须分开的量（它们回答的是不同问题）：

| 信号 | 含义 | RCA 拿它干什么 |
|---|---|---|
| `dependency_graph` | 它**依赖谁**（downstream） | 排查"是不是下游拖垮了它" |
| `blast_radius` | **谁依赖它**（upstream） | 评估"它坏了会连带炸多大一片"（爆炸半径） |

★ 为什么仍然可能"没查成"：`topology_read` 的实现走注入的 `TopologyStore`
  （已约定的接口）。没注入时工具**如实降级**（`degraded=True`，不是返回空）——
  于是这一路会进 `degraded_signals`，而不是被当成"没有依赖关系"。
"""

from __future__ import annotations

from typing import Any, Final, Mapping, Sequence

from ..common.contracts import Evidence
from ..common.enums import EvidenceDirection, EvidenceSource
from .base import AgentContext, ForensicsAgent, ToolRequest, target_services

__all__ = ["TopologyAgent", "TOPO_DEPTH", "MAX_EDGES_IN_SUMMARY"]

#: 跳数：1 跳只能看到直接邻居，"爆炸半径"至少要看 2 跳
TOPO_DEPTH: Final[int] = 2

#: 摘要里最多列几条边（完整边表在黑板证据的 raw_ref 指向的那次查询里）
MAX_EDGES_IN_SUMMARY: Final[int] = 6

#: 最多对几个服务做拓扑查询
_MAX_SERVICES: Final[int] = 3


class TopologyAgent(ForensicsAgent):
    batch_limit = _MAX_SERVICES
    """拓扑取证：上下游是谁、爆炸半径多大。"""

    role = "topology_agent"
    source = EvidenceSource.TOPOLOGY
    tools = ("topology_read",)

    def plan_calls(self, ctx: AgentContext) -> list[ToolRequest]:
        names = target_services(ctx, limit=_MAX_SERVICES)
        return [ToolRequest(
            tool="topology_read",
            params={"service": name, "direction": "both", "depth": TOPO_DEPTH},
            signal="dependency_graph",
            rationale=(f"取 {name} 的 {TOPO_DEPTH} 跳上下游（direction=both）："
                       f"一路给「它依赖谁」，一路给「谁依赖它」（爆炸半径）"))
            for name in names[:_MAX_SERVICES]]

    # ------------------------------------------------------------------
    def build_evidence(self, ctx: AgentContext, request: ToolRequest,
                       result: Any) -> list[Evidence]:
        data = getattr(result, "data", None)
        edges = [edge for edge in ((data or {}).get("edges") or [])
                 if isinstance(edge, Mapping)] if isinstance(data, Mapping) else []
        if not edges:
            return []

        service = str(request.params.get("service") or "")
        entity = ctx.entity(service) or ctx.entity()
        if entity is None:
            return []

        # ★ 边的端点可能是别名（`order`）也可能是规范 id（`svc:order@compose-cn-1`）——
        #   两种都算"同一个服务"（工具层拼 id 的规则不该泄漏到取证逻辑里）
        outgoing = [edge for edge in edges
                    if _same_service(str(edge.get("from") or ""), service, entity.id)]
        incoming = [edge for edge in edges
                    if _same_service(str(edge.get("to") or ""), service, entity.id)]
        raw_ref = getattr(result, "raw_ref", "") or request.tool
        out: list[Evidence] = []

        if outgoing:
            out.append(self.make_evidence(
                ctx, signal="dependency_graph", direction=EvidenceDirection.FLAT,
                onset=ctx.window_start, magnitude=float(len(outgoing)),
                magnitude_unit="条依赖", entity=entity, confidence=0.75,
                raw_ref=f"{raw_ref} | edges={len(edges)}",
                summary=(f"{service} 依赖 {len(outgoing)} 个下游："
                         f"{_edge_text(outgoing, key='to')}"
                         f"{_more(outgoing)}（{TOPO_DEPTH} 跳内，direction=downstream）"),
                tags=["topology", "downstream"]))

        if incoming:
            out.append(self.make_evidence(
                ctx, signal="blast_radius", direction=EvidenceDirection.FLAT,
                onset=ctx.window_start, magnitude=float(len(incoming)),
                magnitude_unit="个上游", entity=entity, confidence=0.75,
                raw_ref=f"{raw_ref} | edges={len(edges)}",
                summary=(f"{service} 若不可用，会影响 {len(incoming)} 个上游："
                         f"{_edge_text(incoming, key='from')}{_more(incoming)}"
                         f"（爆炸半径，direction=upstream）"),
                tags=["topology", "blast_radius"]))
        return out


# ======================================================================
def _same_service(endpoint: str, service: str, entity_id: str) -> bool:
    """边的端点与"我要查的那个服务"是否同一个（容忍别名与规范 id 两种写法）。"""
    if not endpoint:
        return False
    if endpoint in (service, entity_id):
        return True
    head = endpoint.split("@", 1)[0]                  # `svc:order`
    want = entity_id.split("@", 1)[0]
    return head == want or head.endswith(":" + service)


def _edge_text(edges: Sequence[Mapping[str, Any]], *, key: str) -> str:
    return "、".join(str(edge.get(key)) for edge in edges[:MAX_EDGES_IN_SUMMARY])


def _more(edges: Sequence[Mapping[str, Any]]) -> str:
    extra = len(edges) - MAX_EDGES_IN_SUMMARY
    return f" 等 {len(edges)} 个" if extra > 0 else ""
