"""拓扑事实的**来源**（从哪知道"有哪些服务、谁依赖谁"）。

★ 为什么把"来源"单独抽出来：它要落地的是一条链路：

```
被观测系统（事实的持有者）  →  来源  →  拓扑表  →  TopologyStore  →  topology_read
```

其中"来源"有两个实现，**它们的差别不是技术差别，而是事实的权威性差别**：

| 来源 | 它在哪 | 权威性 | 何时可用 |
|---|---|---|---|
| `SelfReportSource` | 被观测系统的 `GET /services`（SDK 自报） | ★ 权威（部署事实归被观测系统） | 等 SDK 把身份与依赖自报上来 |
| `RepoConfigSource` | 同仓 `src/observed/app.yaml` 的 `services.*` | 开发期便利（**Agent 去读被观测系统的配置文件**） | 今天就能用 |

★ 为什么仍然保留 `RepoConfigSource`：本项目现在的被观测系统与服务清单**同仓同机**
  （`discovery` 已经在用 `app.yaml` 的 `services.*.port` 做种子，见 `probe.load_seed`），
  所以它能当天可用，不必等 SDK 自报。
  但它的来源会被如实写进 `identity_source` / `source` 列（`repo:app.yaml(dev)`），
  而且 `SelfReportSource` 一旦有数据就**优先**用它 —— 免得"配置文件抄一份"
  悄悄变成真正的来源（手工清单必然过期）。

★ 两个实现产出**同一个形状**（`TopologySnapshot`），所以换来源只换注入，
  存储与查询一行都不用改。
"""

from __future__ import annotations

from dataclasses import dataclass

import logging
from pathlib import Path
from typing import Any, Final, Mapping, Optional, Protocol, Sequence

from ..common.contracts import ENTITY_ID_PREFIX, EntityType

__all__ = [
    "TopologyNode",
    "TopologyLink",
    "TopologySnapshot",
    "TopologySource",
    "SelfReportSource",
    "RepoConfigSource",
    "pick_source",
    "SOURCE_SELF_REPORT",
    "SOURCE_REPO_CONFIG",
]

logger = logging.getLogger("agent.topology.source")

SOURCE_SELF_REPORT: Final[str] = "self_report"
SOURCE_REPO_CONFIG: Final[str] = "repo:app.yaml(dev)"

#: 跨进程/跨机器时不能读本地文件 → 这里只做"同仓"读取，路径由调用方给
DEFAULT_REPO_CONFIG: Final[Path] = (
    Path(__file__).resolve().parents[2] / "observed" / "app.yaml")


# ======================================================================
@dataclass(frozen=True)
class TopologyNode:
    """一个服务节点（**集群名可空** —— 没拿到就是 None）。"""

    name: str
    cluster: Optional[str] = None
    env: str = "prod"
    namespace: Optional[str] = None
    url: Optional[str] = None
    alive: bool = True

    @property
    def entity_id(self) -> str:
        """规范实体键：有集群 → `svc:order@c1`；没集群 → `svc:order`。

        ★ 集群缺失时**不发明** `@unknown` 这类占位值：占位值一旦进库，
          就再也分不清"真的叫 unknown 的集群"和"没拿到集群名"。
        """
        prefix = ENTITY_ID_PREFIX[EntityType.SERVICE.value]
        return f"{prefix}:{self.name}@{self.cluster}" if self.cluster else f"{prefix}:{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "cluster": self.cluster, "env": self.env,
                "namespace": self.namespace, "url": self.url, "alive": self.alive,
                "entity_id": self.entity_id}


@dataclass(frozen=True)
class TopologyLink:
    """一条依赖边（用**服务名**表达：与 `services.*.downstream` 的键同一套词汇）。

    ★ 用名字而不是实体键，是因为**来源本身就只有名字**
      （`downstream` 的键是 `order`/`payment`）—— 强行在这里拼实体键，
      等于把"集群名"这个未知量硬塞进事实里。

    ★ 如实说明它的边界：名字表达的图**表达不了"同名服务跨集群"**
      （两个集群各有一个 `order` 时，名字无法区分）。将来做多集群时，
      边必须带上集群；存储层不受影响（它本来就是按实体键建的，
      多集群直接 `upsert_dependency(svc:order@c1, …)` 即可）。
    """

    from_name: str
    to_name: str
    call_type: str = "http"
    criticality: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        return {"from": self.from_name, "to": self.to_name,
                "call_type": self.call_type, "criticality": self.criticality}


@dataclass(frozen=True)
class TopologySnapshot:
    """一次拓扑取数的完整结果（来源 + 节点 + 边）。"""

    source: str
    nodes: tuple[TopologyNode, ...] = ()
    links: tuple[TopologyLink, ...] = ()
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "note": self.note,
                "nodes": [node.to_dict() for node in self.nodes],
                "links": [link.to_dict() for link in self.links]}

    def render(self) -> str:
        lines = [f"来源: {self.source}", f"服务: {len(self.nodes)} 个", f"依赖边: {len(self.links)} 条"]
        for node in self.nodes:
            cluster = node.cluster or "（未提供集群名）"
            lines.append(f"  · {node.name:12s} {node.entity_id:28s} cluster={cluster}")
        for link in self.links:
            lines.append(f"  → {link.from_name} → {link.to_name} ({link.call_type})")
        if self.note:
            lines.append(f"说明: {self.note}")
        return "\n".join(lines)


# ======================================================================
class TopologySource(Protocol):
    """拓扑事实的来源（**只负责取数，不负责落库**）。"""

    name: str

    async def fetch(self) -> TopologySnapshot:  # pragma: no cover - 协议
        ...

    def available(self) -> tuple[bool, str]:
        """`(够不够用, 说明)` —— 用来决定"能不能用它"。"""
        ...


# ======================================================================
#  ① 自报（权威来源；SDK 自报依赖之后成为主路径）
# ======================================================================
@dataclass
class SelfReportSource:
    """读 `observed_info.yaml`（被观测系统自报）里的身份与依赖。

    ★ 现在 `observed_info.yaml` 里**只有名字与地址**，没有 cluster / depends_on，
      所以本实现会如实返回"节点有、边为空"，并把缺什么写进 `note` ——
      而不是假装取到了。等 SDK 把这些字段自报上来，这里不需要改一行
      （解析顺序已经按"有就用"写好）。
    """

    info: Optional[Any] = None            # `discovery.probe.ObservedInfo`
    name: str = SOURCE_SELF_REPORT

    def available(self) -> tuple[bool, str]:
        """★ 判据是"能不能回答**依赖关系**"，不是"有没有服务清单"。

        为什么这么定：拓扑的用处就是"上下游 + 爆炸半径"。自报里只有服务清单、
        没有 `depends_on` 时，用它只会得到一张**没有边的图** ——
        那种"可用"会把配置来源挤掉，然后让拓扑这一路报出"没有依赖关系"
        （假阴性）。所以这里如实返回不可用，并把缺什么写清楚。
        """
        if self.info is None or not getattr(self.info, "services", None):
            return False, "没有观察到的服务清单（先跑 `observed-discover`）"
        has_edges = any(_attr(service, "depends_on")
                        for service in getattr(self.info, "services", ()) or ())
        if not has_edges:
            return False, "自报里只有服务清单、**没有依赖关系**（缺 `depends_on`，等 SDK 自报）"
        return True, "ok"

    async def fetch(self) -> TopologySnapshot:
        nodes: list[TopologyNode] = []
        links: list[TopologyLink] = []
        for service in getattr(self.info, "services", ()) or ():
            cluster = _attr(service, "cluster")
            node = TopologyNode(
                name=str(_attr(service, "name")),
                cluster=str(cluster) if cluster else None,
                env=str(_attr(service, "env") or "prod"),
                namespace=_attr(service, "namespace"),
                url=_attr(service, "url"),
                alive=bool(_attr(service, "alive")),
            )
            nodes.append(node)
            for target in _attr(service, "depends_on") or ():
                links.append(TopologyLink(from_name=node.name, to_name=str(target)))

        missing = [what for what, present in (
            ("cluster", any(node.cluster for node in nodes)),
            ("depends_on", bool(links))) if not present]
        note = ("自报里缺：" + "、".join(missing) + "（等 SDK 自报）") if missing else ""
        return TopologySnapshot(source=self.name, nodes=tuple(nodes),
                                links=tuple(links), note=note)


# ======================================================================
#  ② 同仓配置文件（开发期便利，来源如实标注）
# ======================================================================
@dataclass
class RepoConfigSource:
    """读同仓 `src/observed/app.yaml` 的 `services.*.port` 与 `services.*.downstream`。

    ★ 与 `discovery.probe._seed_from_repo_config` 读的是同一段（`services.*.port`），
      区别只是这里还读了 `downstream` —— 依赖关系的**唯一声明处**就在那份配置里
      （URL 只声明一处；`downstream` 的键就是"调用谁"）。
    """

    path: Path = DEFAULT_REPO_CONFIG
    cluster: Optional[str] = None          # 部署事实；没给就留空（不发明）
    env: str = "prod"
    namespace: Optional[str] = None
    name: str = SOURCE_REPO_CONFIG

    def available(self) -> tuple[bool, str]:
        if not self.path.exists():
            return False, f"读不到 {self.path}"
        return True, "ok"

    async def fetch(self) -> TopologySnapshot:
        import yaml  # noqa: PLC0415 —— 只有这条路径需要 YAML

        try:
            doc = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"解析 {self.path} 失败：{type(exc).__name__}: {exc}") from exc

        services = doc.get("services") or {}
        if not isinstance(services, Mapping):
            raise ValueError(f"{self.path} 的 `services` 段不是映射")

        nodes: list[TopologyNode] = []
        links: list[TopologyLink] = []
        for name, block in services.items():
            entry = block if isinstance(block, Mapping) else {}
            port = entry.get("port")
            node = TopologyNode(
                name=str(name), cluster=self.cluster, env=self.env,
                namespace=self.namespace,
                url=_local_url(port) if port else None, alive=True)
            nodes.append(node)
            downstream = entry.get("downstream") or {}
            if isinstance(downstream, Mapping):
                for target in downstream:
                    links.append(TopologyLink(from_name=str(name), to_name=str(target)))

        unknown = sorted({link.to_name for link in links} - {node.name for node in nodes})
        note = ""
        if unknown:
            note = f"下游里出现了未声明的服务：{unknown}（已保留边，但那些节点没有地址）"
        return TopologySnapshot(source=self.name, nodes=tuple(nodes),
                                links=tuple(links), note=note)


def _local_url(port: Any) -> Optional[str]:
    try:
        return f"http://127.0.0.1:{int(port)}"
    except (TypeError, ValueError):
        return None


def _attr(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


# ======================================================================
def pick_source(sources: Sequence[TopologySource]) -> tuple[Optional[TopologySource], str]:
    """按**权威性顺序**选一个可用的来源（自报优先），并给出理由。

    ★ 顺序不可反：`SelfReportSource` 在列表前面。哪怕两个都可用，
      也必须用被观测系统自报的那份 —— 否则"Agent 读配置文件"就成了真正的来源，
      而手工/同仓清单**必然过期**。
    """
    reasons: list[str] = []
    for source in sources:
        ok, why = source.available()
        reasons.append(f"{source.name}: {'可用' if ok else why}")
        if ok:
            return source, "；".join(reasons)
    return None, "；".join(reasons)
