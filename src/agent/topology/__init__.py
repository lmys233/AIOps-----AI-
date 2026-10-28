"""拓扑层 —— `TopologyStore` 接口的当前实现。

| 模块 | 职责 |
|---|---|
| `source.py` | **来源**：`SelfReportSource`（被观测系统自报，权威）/ `RepoConfigSource`（同仓配置，开发期便利） |
| `store.py` | **存储**：`SqlTopologyStore`（Postgres 两张表）+ `InMemoryTopologyStore`；遍历与解析逻辑**只有一份** |
| `schema.sql` | 两张表（**Agent 自己的表**，不属于存储契约） |
| `__main__.py` | `topology-migrate` / `topology-sync` / `topology-check` |

★ 这一层的立场：**集群名是限定符，服务名才是定位符** ——
  拿不到集群名时，解析与遍历照常工作（见 `store.parse_entity_key` / `resolve`）。
"""

from __future__ import annotations

from .source import (
    SOURCE_REPO_CONFIG,
    SOURCE_SELF_REPORT,
    RepoConfigSource,
    SelfReportSource,
    TopologyLink,
    TopologyNode,
    TopologySnapshot,
    TopologySource,
    pick_source,
)
from .store import (
    MAX_TOPOLOGY_DEPTH,
    InMemoryTopologyStore,
    SqlTopologyStore,
    TopologyAmbiguous,
    TopologyStoreBase,
    TopologyUnavailable,
    parse_entity_key,
    walk_edges,
)

__all__ = [
    "TopologySource",
    "TopologySnapshot",
    "TopologyNode",
    "TopologyLink",
    "SelfReportSource",
    "RepoConfigSource",
    "pick_source",
    "SOURCE_SELF_REPORT",
    "SOURCE_REPO_CONFIG",
    "TopologyStoreBase",
    "SqlTopologyStore",
    "InMemoryTopologyStore",
    "TopologyUnavailable",
    "TopologyAmbiguous",
    "walk_edges",
    "parse_entity_key",
    "MAX_TOPOLOGY_DEPTH",
]
