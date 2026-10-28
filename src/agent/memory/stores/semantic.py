"""语义记忆（服务拓扑 —— ★ 唯一来源是 `TopologyStore`，本层不另存副本）。

★ 本文件由原先的单文件 `stores.py`（903 行）拆分而来；
  **公开导入面一字未改**（包的 `__init__.py` 显式再导出）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .base import MemoryUnavailable
from .records import SemanticFact


# ======================================================================
#  ③ 语义记忆（委托 TopologyStore —— 不另存一份）
# ======================================================================
@dataclass
class SemanticMemory:
    """语义记忆：包装 `TopologyStore`（随 CMDB 同步）。"""

    topology: Any = None

    async def recall(self, *, entity_id: Optional[str], top_k: int = 3) -> list[SemanticFact]:
        if self.topology is None or not entity_id:
            return []
        try:
            downstream = await self.topology.get_dependencies(entity_id, direction="downstream", depth=1)
            upstream = await self.topology.get_dependencies(entity_id, direction="upstream", depth=1)
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailable("semantic", f"拓扑查询失败：{type(exc).__name__}") from exc
        facts: list[SemanticFact] = []
        if downstream:
            names = "、".join(sorted({getattr(e, "to_service", "?") for e in downstream}))
            facts.append(SemanticFact(subject=entity_id, statement=f"{entity_id} 依赖：{names}"))
        if upstream:
            names = "、".join(sorted({getattr(e, "from_service", "?") for e in upstream}))
            facts.append(SemanticFact(subject=entity_id, statement=f"{entity_id} 被这些服务依赖（爆炸半径）：{names}"))
        return facts[:top_k]

# 兼容别名（读起来更清楚时用这个名字）
TopologySemanticMemory = SemanticMemory
