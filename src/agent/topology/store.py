"""`TopologyStore` 的当前实现（先用 Postgres 两张表，日后可换 Neo4j）。

★ 三条设计选择，都是"当前规模 + 诚实"推出来的：

**① 遍历放在 Python 里做，SQL 只负责"把边全取回来"**

服务图里**有环**（A→B→A 在多服务互调里很常见）。用递归 CTE 走环要靠
`UNION` 去重 + 跳数上限，写对了也不容易看懂；而当前规模是"十来个服务、
几十条边"，一次全量读入内存做 BFS 既正确又便宜。所以：

· `_walk()` 是一个**纯函数**，`SqlTopologyStore` 与 `InMemoryTopologyStore`
  **共用它** —— 两种实现的遍历语义不可能漂移（这正是单测能同时覆盖两者的原因）。

**② 服务 → 实体解析**与集群名无关**（这是"拿不到集群名也能工作"的落点）**

`order` / `svc:order` / `svc:order@compose-cn-1` 三种写法都解析到同一个实体：
定位靠**服务名**，集群名只是**限定符**。而当同名服务真的存在于多个集群、
查询又没指定集群时 —— **不猜**，抛 `TopologyAmbiguous`（与指代消解"歧义不猜"同一口径），
由工具层翻译成 `QueryRejected`（"查询写错了，可以自己修"），提示调用方补集群名。

**③ 对外返回**服务名**，不返回实体键**

`DependencyEdge.from_service/to_service` 给的是名字（`order` → `payment`）：
这与被观测系统自己的词汇一致（`services.*.downstream` 的键就是名字），
读起来也是人话。集群维度只在**解析**阶段用来消歧。
（多集群同构部署时"哪条边属于哪个集群"需要实体键 —— 届时把 id 一并带出来即可，
 存储里本来就有。）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Any, Callable, Final, Iterable, Mapping, Optional, Protocol,
                    Sequence, runtime_checkable)

from ..common.interfaces import DependencyEdge, TopologyStore
from .source import TopologyNode, TopologySnapshot

__all__ = [
    "TopologyUnavailable",
    "TopologyAmbiguous",
    "TopologyStoreBase",
    "TopologyDatabase",
    "SqlTopologyStore",
    "InMemoryTopologyStore",
    "walk_edges",
    "parse_entity_key",
    "set_database_factory",
    "MAX_TOPOLOGY_DEPTH",
    "SCHEMA_PATH",
    "METRICS_AGENT_ROLE",
]

logger = logging.getLogger("agent.topology")

SCHEMA_PATH: Final[Path] = Path(__file__).resolve().parent / "schema.sql"

#: 跳数最多 3（更大的范围请分多次查）
MAX_TOPOLOGY_DEPTH: Final[int] = 3

#: QPS 的来源标记。★ 这是**角色名**（与 `common.enums.AGENT_ROLES` 里的同名），
#: 不是随手写的字符串：来源标记进的是契约字段，改名要跟着角色矩阵一起改。
METRICS_AGENT_ROLE: Final[str] = "metrics_agent"


class TopologyUnavailable(Exception):
    """拓扑存储不可用（连不上/没建表）→ **没查成**（`degraded=True`）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class TopologyAmbiguous(Exception):
    """同名服务存在于多个集群、而查询没指定集群 → **查询需要更具体**。

    ★ 与 `TopologyUnavailable` 分开：前者的处置是"补集群名再查"（可自修），
      后者的处置是"接依赖"（人来修）。混成一个会让调用方无从下手。
    """

    def __init__(self, message: str, *, clusters: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.clusters = tuple(clusters)


# ======================================================================
#  纯函数：解析键 / 遍历
# ======================================================================
def parse_entity_key(service_id: str) -> tuple[str, Optional[str]]:
    """`svc:order@compose-cn-1` / `svc:order` / `order` → `(name, cluster 或 None)`。

    ★ 容忍三种写法是**故意的**：不同来源的写法不同（工具入参常给裸名字、
      黑板给规范 id、集群未知时 id 里根本没有 `@`），解析不该挑食。
    """
    text = str(service_id or "").strip()
    if not text:
        return "", None
    head, _, tail = text.partition("@")
    if ":" in head:
        _, _, head = head.partition(":")
    return head.strip(), (tail.strip() or None)


def walk_edges(edges: Iterable[Mapping[str, Any]], start_keys: Iterable[str], *,
               direction: str = "downstream", depth: int = 1) -> list[dict[str, Any]]:
    """从 `start_keys` 出发做**有界 BFS**，返回触达的边（含 `depth`）。

    `edges` 给的键是**实体键**（`[{"from","to","call_type","criticality"}]`）；
    `direction` 取 `downstream`（它依赖谁）/ `upstream`（谁依赖它）/ `both`；
    `depth` 是跳数，**自动夹到 `MAX_TOPOLOGY_DEPTH`**。

    ★ 环安全：靠 `visited` 集合 + 跳数上限（两者都要，只有上限的话
      "深度 3 的环"仍会被重复走到，返回一堆重复边）。
    """
    hops = max(1, min(int(depth), MAX_TOPOLOGY_DEPTH))
    rows = [dict(edge) for edge in edges]
    frontier = {str(key) for key in start_keys if key}
    visited: set[str] = set(frontier)
    out: list[dict[str, Any]] = []

    for level in range(1, hops + 1):
        found: list[dict[str, Any]] = []
        for edge in rows:
            forward = str(edge.get("from") or "")
            backward = str(edge.get("to") or "")
            if direction in ("downstream", "both") and forward in frontier:
                found.append({**edge, "depth": level, "anchor": forward})
            elif direction in ("upstream", "both") and backward in frontier:
                found.append({**edge, "depth": level, "anchor": backward})
        if not found:
            break

        next_frontier: set[str] = set()
        for edge in found:
            forward, backward = str(edge.get("from")), str(edge.get("to"))
            other = backward if edge["anchor"] == forward else forward
            # 起点自己不算"上下游"
            if other in visited or other in {str(key) for key in start_keys}:
                continue
            next_frontier.add(other)
            out.append(edge)
        if not next_frontier:
            break
        visited |= next_frontier
        frontier = next_frontier

    return out


def _qps_summary(estimates: Mapping[str, float], upstream: set[str]
                 ) -> tuple[float, str, str]:
    """QPS 汇总 → `(qps_affected, qps_source, note)`，**来源是三态**。

    ★ QPS 只能来自入参（拓扑侧拿不到 QPS，不能自己编），于是只有三种结论，
      三者不许混 —— 混了就等于把"没查到"说成"查到了"：

      | `qps_source` | 含义 |
      |---|---|
      | `metrics_agent` | 查到了：入参里**至少有一个名字**命中了上游服务 |
      | `metrics_agent_no_match` | 查了但没有：拿到了 QPS，却一个上游服务都没命中 |
      | `unavailable` | 没查：调用方没给 QPS |

      后两种的 `qps_affected` 都是 0，但含义完全不同（"这条上游没流量" vs
      "我们没拿到它的流量"），所以 `note` 必须把这句说出来。
    """
    values = {str(name): value for name, value in dict(estimates or {}).items()}
    matched = sorted(name for name in upstream if name in values)
    total = round(sum(float(values[name]) for name in matched), 3)
    if not values:
        return total, "unavailable", ("调用方没提供 QPS（`qps_estimates` 为空）→ "
                                      "qps_affected 计 0，**不代表这些上游没有流量**")
    if matched:
        unused = sorted(set(values) - set(upstream))
        tail = (f"；入参里这些名字没有对应的上游服务、未被计入：{'、'.join(unused)}"
                if unused else "")
        return total, METRICS_AGENT_ROLE, f"QPS 命中 {len(matched)}/{len(upstream)} 个上游服务{tail}"
    return total, f"{METRICS_AGENT_ROLE}_no_match", (
        f"**查了但没有命中**：入参 {'、'.join(sorted(values))} 与受影响的上游 "
        f"{'、'.join(sorted(upstream)) or '（空集）'} 没有重合 → qps_affected 计 0，"
        f"**不代表这些上游没有流量**")


# ======================================================================
#  共用底座：解析 + 遍历 + 爆炸半径（两种存储只差"怎么读写"）
# ======================================================================
class TopologyStoreBase(TopologyStore):
    """把"存储无关"的部分集中在这里：解析、遍历、爆炸半径的算法**只有一份**。"""

    # ---- 子类实现这四个 ----
    async def _load_services(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def _load_edges(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def _write_service(self, row: Mapping[str, Any]) -> None:
        raise NotImplementedError

    async def _write_edge(self, row: Mapping[str, Any]) -> None:
        raise NotImplementedError

    # ---- 公共实现 ----
    async def upsert_service(self, service_id: str, name: str, *, env: str = "prod",
                             cluster: Optional[str] = None, owner: Optional[str] = None,
                             tier: Optional[int] = None,
                             metadata: Optional[dict[str, Any]] = None,
                             **extra: Any) -> None:
        """写入/更新一个服务节点（幂等）。

        `extra` 接本实现额外的列（`namespace` / `url` / `alive` / `identity_source`）；
        ★ 它们是**可选的**：接口没规定，所以不能强迫调用方给。
        """
        row = {
            "entity_id": service_id,
            "name": name,
            "cluster": cluster,
            "env": env or "prod",
            "namespace": extra.get("namespace"),
            "url": extra.get("url"),
            "alive": bool(extra.get("alive", True)),
            "owner": owner,
            "tier": tier,
            "identity_source": str(extra.get("identity_source") or "unknown"),
            "metadata": dict(metadata or {}),
        }
        await self._write_service(row)

    async def upsert_dependency(self, edge: DependencyEdge, **extra: Any) -> None:
        await self._write_edge({
            "from": str(edge.from_service), "to": str(edge.to_service),
            "call_type": edge.call_type or "http",
            "criticality": edge.criticality or "medium",
            "source": str(extra.get("source") or "unknown"),
        })

    async def resolve(self, service_id: str) -> list[dict[str, Any]]:
        """服务 → 服务行（**与集群名无关**；多集群歧义 → `TopologyAmbiguous`）。"""
        name, cluster = parse_entity_key(service_id)
        if not name:
            raise TopologyAmbiguous("空的实体键，无法解析 —— 请给出服务名")

        services = await self._load_services()
        if any(str(row["entity_id"]) == str(service_id) for row in services):
            return [row for row in services if str(row["entity_id"]) == str(service_id)]

        matched = [row for row in services if str(row["name"]) == name]
        if not matched:
            return []
        if cluster:
            exact = [row for row in matched
                     if str(row.get("cluster") or "") == cluster]
            if exact:
                return exact
            # ★ 库里**没记**集群名 → 与任何集群都兼容（单集群 / 未自报的常态）
            unknown = [row for row in matched if not row.get("cluster")]
            if unknown:
                return unknown
            # ★ 明确属于**别的**集群 → 返回空（"查了没这个服务"），
            #   而不是拿另一个集群的数据顶上（那会静默给出错误的依赖图）
            return []
        if len(matched) > 1:
            clusters = sorted({str(row.get("cluster") or "（未提供集群名）") for row in matched})
            raise TopologyAmbiguous(
                f"{name} 同时存在于 {len(matched)} 个集群 {clusters} —— "
                f"**不猜**：请在查询里指定 cluster 后重试",
                clusters=clusters)
        return matched

    async def get_dependencies(self, service_id: str, direction: str = "downstream",
                               depth: int = 1) -> Sequence[DependencyEdge]:
        rows = await self.resolve(service_id)
        if not rows:
            return []                        # ★ "查了没这个服务" = 空结果，不是错误
        keys = [str(row["entity_id"]) for row in rows]
        edges = await self._load_edges()
        walked = walk_edges(edges, keys, direction=direction, depth=depth)
        id_to_name = {str(row["entity_id"]): str(row["name"])
                      for row in await self._load_services()}
        out: list[DependencyEdge] = []
        seen: set[tuple[str, str]] = set()
        for edge in walked:
            pair = (str(edge["from"]), str(edge["to"]))
            if pair in seen:
                continue
            seen.add(pair)
            out.append(DependencyEdge(
                from_service=id_to_name.get(pair[0], pair[0]),
                to_service=id_to_name.get(pair[1], pair[1]),
                call_type=str(edge.get("call_type") or "http"),
                criticality=str(edge.get("criticality") or "medium")))
        return out

    async def blast_radius(self, service_id: str,
                           qps_estimates: Optional[dict[str, float]] = None
                           ) -> dict[str, Any]:
        """爆炸半径：**谁依赖它**（上游闭包）+ 受影响服务数 + QPS 汇总。

        QPS **只能来自入参**（拓扑侧拿不到 QPS，不能自己编），来源分**三态**
        —— "查到了 / 查了没有 / 没查"不许混，判据与话术见 `_qps_summary`。
        """
        rows = await self.resolve(service_id)
        if not rows:
            return {"services_affected": 0, "upstream_count": 0, "qps_affected": 0.0,
                    "downstream_count": 0, "qps_source": "unavailable",
                    "note": f"库里没有 {service_id}（连上游都算不出来）"}
        keys = [str(row["entity_id"]) for row in rows]
        edges = await self._load_edges()
        id_to_name = {str(row["entity_id"]): str(row["name"])
                      for row in await self._load_services()}
        upstream = {id_to_name.get(str(edge["from"]), str(edge["from"]))
                    for edge in walk_edges(edges, keys, direction="upstream",
                                           depth=MAX_TOPOLOGY_DEPTH)}
        downstream = {id_to_name.get(str(edge["to"]), str(edge["to"]))
                      for edge in walk_edges(edges, keys, direction="downstream",
                                             depth=MAX_TOPOLOGY_DEPTH)}
        qps_affected, qps_source, note = _qps_summary(qps_estimates or {}, upstream)
        return {"services_affected": len(upstream), "upstream_count": len(upstream),
                "downstream_count": len(downstream), "qps_affected": qps_affected,
                "qps_source": qps_source, "note": note, "upstream": sorted(upstream)}

    async def health(self) -> tuple[bool, str]:
        try:
            services = await self._load_services()
            edges = await self._load_edges()
        except TopologyUnavailable as exc:
            return False, exc.message
        return True, f"ok：{len(services)} 个服务 / {len(edges)} 条依赖边"

    # ---- 同步（把一次快照灌进存储）----
    async def apply(self, snapshot: TopologySnapshot) -> dict[str, int]:
        """把来源快照落库（幂等：同键覆盖）。返回写入计数。"""
        by_name: dict[str, TopologyNode] = {node.name: node for node in snapshot.nodes}
        keep: set[str] = {node.entity_id for node in snapshot.nodes}
        for node in snapshot.nodes:
            await self.upsert_service(
                node.entity_id, node.name, env=node.env, cluster=node.cluster,
                namespace=node.namespace, url=node.url, alive=node.alive,
                identity_source=snapshot.source)
        written_edges = 0
        for link in snapshot.links:
            head = by_name.get(link.from_name)
            tail = by_name.get(link.to_name)
            if head is None:
                logger.warning("拓扑同步：边 %s → %s 的起点不在清单里，跳过",
                               link.from_name, link.to_name)
                continue
            if tail is None:
                # ★ 下游没被声明时**仍然保留这条边**（它是一条真实的依赖），
                #   但用一个"未登记"节点占位 —— 不假装它有地址。
                tail = TopologyNode(name=link.to_name, cluster=head.cluster,
                                    env=head.env, alive=False)
                by_name[link.to_name] = tail
                keep.add(tail.entity_id)
                await self.upsert_service(tail.entity_id, tail.name, env=tail.env,
                                          cluster=tail.cluster, alive=False,
                                          identity_source=f"{snapshot.source}:edge-only")
            await self.upsert_dependency(
                DependencyEdge(head.entity_id, tail.entity_id, link.call_type,
                               link.criticality), source=snapshot.source)
            written_edges += 1
        # ★ 清理时**保留占位节点**（`keep` 已经把它们算进去），
        #   否则"未声明的下游"那条边刚写进去就被删掉。
        await self.prune(keep)
        return {"services": len(keep), "edges": written_edges}

    async def prune(self, keep: Iterable[str]) -> int:
        """删掉**不在 `keep` 里**的节点与边（拓扑是"当前态"，不是历史账本）。

        ★ 为什么必须删：否则一次配置变更后，旧边会永远留在图里，
          "爆炸半径"会越算越大 —— 而它直接进风险分级。
        """
        return 0

    async def snapshot(self) -> dict[str, Any]:
        return {"services": await self._load_services(),
                "edges": await self._load_edges()}


# ======================================================================
#  数据库能力（窄接口 + 注入点）
# ======================================================================
@runtime_checkable
class TopologyDatabase(Protocol):
    """拓扑存储要的那点数据库能力：跑查询、跑改动、关连接池。

    ★ 为什么单独写出来：拓扑层只需要这三件事，**不该知道那份连接池封装住在哪一层**
      （它是记忆层的实现细节）。装配点注入现成对象或工厂，本层就能被独立测。
    """

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        """查询，返回行字典的列表。"""

    async def execute(self, sql: str, *args: Any) -> str:
        """执行，返回驱动给的统计串（例如 `DELETE 3`）。"""

    async def close(self) -> None:
        """关掉连接池。"""


#: 数据库工厂（注入点），签名 `(dsn) -> TopologyDatabase`。
_DATABASE_FACTORY: Optional[Callable[[str], Any]] = None


def set_database_factory(factory: Optional[Callable[[str], Any]]) -> None:
    """换掉"默认用哪份数据库封装"（传 `None` 恢复默认）。"""
    global _DATABASE_FACTORY
    _DATABASE_FACTORY = factory


def _default_database(dsn: str) -> Any:
    """没注入 `db` / `db_factory` 时的默认连接池。

    ★ 这是本层**唯一**一处对记忆层实现的引用，且只在"两者都没给"时用到 ——
      如实记录当前事实：连接池封装目前只有记忆层那一份。
      要彻底断开这条边，得把它提到 `common/`（两边都 import 它），
      那会动 `common/` 与 `memory/`，不在本次改动边界内。
    """
    if _DATABASE_FACTORY is not None:
        return _DATABASE_FACTORY(dsn)
    from ..memory.service import PgDatabase  # noqa: PLC0415

    return PgDatabase(dsn=dsn)


# ======================================================================
#  Sql 实现
# ======================================================================
@dataclass
class SqlTopologyStore(TopologyStoreBase):
    """Postgres 实现（数据库**注入**：`db=` 现成对象 / `db_factory=` 工厂）。"""

    db: Any = None
    dsn: str = ""
    #: 注入点：`(dsn) -> TopologyDatabase`；不传就用默认连接池封装
    db_factory: Optional[Callable[[str], Any]] = None
    _cache: Any = field(default=None, init=False, repr=False)

    async def _database(self) -> Any:
        if self.db is not None:
            return self.db
        self.db = (self.db_factory(self.dsn) if self.db_factory is not None
                   else _default_database(self.dsn))
        return self.db

    async def migrate(self) -> int:
        """执行 `schema.sql`（幂等）。返回语句数。"""
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        db = await self._database()
        try:
            await db.execute(sql)
        except Exception as exc:  # noqa: BLE001
            raise TopologyUnavailable(
                f"建拓扑表失败（{type(exc).__name__}）：{exc}") from exc
        statements = len([part for part in sql.split(";") if part.strip()])
        return statements

    # ---- 读写 ----
    async def _load_services(self) -> list[dict[str, Any]]:
        db = await self._database()
        try:
            return await db.fetch(
                "SELECT entity_id, name, cluster, env, namespace, url, alive, owner, "
                "tier, identity_source, metadata FROM topology_services")
        except Exception as exc:  # noqa: BLE001
            raise TopologyUnavailable(
                f"读拓扑服务表失败（{type(exc).__name__}）：{exc}") from exc

    async def _load_edges(self) -> list[dict[str, Any]]:
        db = await self._database()
        try:
            return await db.fetch(
                "SELECT from_entity_id AS \"from\", to_entity_id AS \"to\", "
                "call_type, criticality, source FROM topology_edges")
        except Exception as exc:  # noqa: BLE001
            raise TopologyUnavailable(
                f"读拓扑边表失败（{type(exc).__name__}）：{exc}") from exc

    async def _write_service(self, row: Mapping[str, Any]) -> None:
        db = await self._database()
        try:
            await db.execute(
                """
                INSERT INTO topology_services
                    (entity_id, name, cluster, env, namespace, url, alive, owner, tier,
                     identity_source, metadata, updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb, now())
                ON CONFLICT (entity_id) DO UPDATE SET
                    name = EXCLUDED.name, cluster = EXCLUDED.cluster, env = EXCLUDED.env,
                    namespace = EXCLUDED.namespace, url = EXCLUDED.url, alive = EXCLUDED.alive,
                    owner = EXCLUDED.owner, tier = EXCLUDED.tier,
                    identity_source = EXCLUDED.identity_source,
                    metadata = EXCLUDED.metadata, updated_at = now()
                """,
                row["entity_id"], row["name"], row["cluster"], row["env"],
                row["namespace"], row["url"], row["alive"], row["owner"], row["tier"],
                row["identity_source"], json.dumps(row["metadata"], ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            # ★ 写路径也要把底层异常翻成 `TopologyUnavailable`：
            #   读路径一直是这么做的，写路径漏了 —— 于是 `sync` 撞上 DB 错误时
            #   抛出的是原始 traceback（`MemoryUnavailable`/asyncpg 异常），
            #   而不是 CLI 统一的「拓扑存储不可用」，用户看不出"是存储挂了还是代码错了"。
            raise TopologyUnavailable(
                f"写拓扑服务表失败（{type(exc).__name__}）：{exc}") from exc

    async def _write_edge(self, row: Mapping[str, Any]) -> None:
        db = await self._database()
        try:
            await db.execute(
                """
                INSERT INTO topology_edges
                    (from_entity_id, to_entity_id, call_type, criticality, source, updated_at)
                VALUES ($1,$2,$3,$4,$5, now())
                ON CONFLICT (from_entity_id, to_entity_id) DO UPDATE SET
                    call_type = EXCLUDED.call_type, criticality = EXCLUDED.criticality,
                    source = EXCLUDED.source, updated_at = now()
                """,
                row["from"], row["to"], row["call_type"], row["criticality"], row["source"])
        except Exception as exc:  # noqa: BLE001
            raise TopologyUnavailable(
                f"写拓扑边表失败（{type(exc).__name__}）：{exc}") from exc

    async def prune(self, keep: Iterable[str]) -> int:
        """删掉本次快照未覆盖的节点与边（拓扑是当前态）。"""
        db = await self._database()
        keys = [str(key) for key in keep]
        if not keys:
            return 0
        removed = 0
        try:
            # 先删边（引用节点），再删孤立节点
            removed += _affected(await db.execute(
                "DELETE FROM topology_edges WHERE from_entity_id <> ALL($1::text[]) "
                "OR to_entity_id <> ALL($1::text[])", keys))
            removed += _affected(await db.execute(
                "DELETE FROM topology_services WHERE entity_id <> ALL($1::text[])", keys))
        except Exception as exc:  # noqa: BLE001
            raise TopologyUnavailable(
                f"清理拓扑旧数据失败（{type(exc).__name__}）：{exc}") from exc
        return removed

    async def close(self) -> None:
        if self.db is not None and hasattr(self.db, "close"):
            await self.db.close()
            self.db = None


def _affected(status: str) -> int:
    """asyncpg 的 `execute` 返回 `DELETE 3` 这类串 —— 取出数字（拿不到就是 0）。"""
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0


# ======================================================================
#  内存实现（离线单测 / 无库时的参考语义）
# ======================================================================
@dataclass
class InMemoryTopologyStore(TopologyStoreBase):
    """内存拓扑。**遍历与解析逻辑与 Sql 版完全共用**（见模块头 ①）。"""

    services: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    edges: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict, repr=False)
    fail_with: Optional[str] = None          # 造"没查成"用

    async def _load_services(self) -> list[dict[str, Any]]:
        if self.fail_with:
            raise TopologyUnavailable(self.fail_with)
        return list(self.services.values())

    async def _load_edges(self) -> list[dict[str, Any]]:
        if self.fail_with:
            raise TopologyUnavailable(self.fail_with)
        return list(self.edges.values())

    async def _write_service(self, row: Mapping[str, Any]) -> None:
        self.services[str(row["entity_id"])] = dict(row)

    async def _write_edge(self, row: Mapping[str, Any]) -> None:
        self.edges[(str(row["from"]), str(row["to"]))] = dict(row)

    async def prune(self, keep: Iterable[str]) -> int:
        wanted = {str(key) for key in keep}
        removed = len([key for key in self.edges
                       if key[0] not in wanted or key[1] not in wanted])
        self.edges = {key: value for key, value in self.edges.items()
                      if key[0] in wanted and key[1] in wanted}
        stale = [key for key in self.services if key not in wanted]
        for key in stale:
            self.services.pop(key, None)
        return removed + len(stale)
