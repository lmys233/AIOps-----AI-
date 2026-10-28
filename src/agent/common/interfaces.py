"""抽象接口。

三个接口的具体实现可替换，**Agent 侧代码零改动**：
    KeywordSearch  : Postgres pg_trgm  →  Elasticsearch（BM25）
    TopologyStore  : Postgres 表        →  Neo4j
    ChangeChannel  : 应用层配置版本化   →  Argo CD（GitOps）

设计要点：

1. **只声明方法签名，不含实现**。
   因此这里用 `abc.ABC` 而非 `typing.Protocol` —— ABC 能在**实例化时**
   就报错（`TypeError: Can't instantiate abstract class`），
   比 Protocol 的静态检查更能防住"忘了实现某个方法却在运行期才发现"。

2. **工具名与存储实现解耦**。
   对外暴露的方法名用 `topology` 语义，**不得**出现 `cypher` 等存储专属命名；
   将来换 Neo4j 时调用方无需改动。

3. **`ChangeChannel.execute()` 内部必须重新校验等级与审批**
   （不信任调用方传入的等级）。
   方法签名因此**不接收** risk_level 参数 —— 从类型层面杜绝调用方"传一个等级进来"。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional, Sequence

from .contracts import ChangeResult, ProposedAction, RiskDecision

__all__ = [
    "KeywordSearch",
    "TopologyStore",
    "ChangeChannel",
    "SearchHit",
    "DependencyEdge",
]


# ==================================================================
#  接口返回值的数据形状（保持轻量，避免与契约对象耦合过深）
# ==================================================================
class SearchHit:
    """关键词检索命中项。

    ★ 为什么不用 dataclass：本接口要允许实现返回任意带属性的对象
      （如 pg_trgm 的行对象 / ES 的 dict 包装），
      因此只约定**属性名**，不约束类型。
    """

    __slots__ = ("doc_id", "score", "text", "metadata")

    def __init__(self, doc_id: str, score: float, text: str,
                 metadata: Optional[dict[str, Any]] = None) -> None:
        self.doc_id = doc_id
        self.score = score
        self.text = text
        self.metadata = metadata or {}


class DependencyEdge:
    """拓扑依赖边：from_service → to_service。"""

    __slots__ = ("from_service", "to_service", "call_type", "criticality")

    def __init__(self, from_service: str, to_service: str,
                 call_type: str = "http", criticality: str = "medium") -> None:
        self.from_service = from_service
        self.to_service = to_service
        self.call_type = call_type
        self.criticality = criticality


# ==================================================================
#  1. KeywordSearch（pg_trgm → ES/BM25）
# ==================================================================
class KeywordSearch(ABC):
    """关键词检索接口 —— 记忆三路召回中的「关键词路」。

    ★★ **这是一份声明，不是一条已接线的通路**：本仓目前**没有**实现类接入它，
      记忆层的关键词路走的是 `memory/stores/episodic.py` 的 `search_keyword`
      （那里用 `Protocol` 声明形状）。保留本接口是为了把"替换点"的签名先定下来：
      关键词路的存储与排序应当可换（Postgres `pg_trgm` + GIN ↔ Elasticsearch/BM25），
      换的时候 Agent 侧代码不动。

    ★ 所以：真要用关键词路请走记忆层的实现；不要因为看见这个类
      就以为"关键词检索已经具备按实现替换的能力"。

    ★ 共同约束：只声明方法签名不含实现 —— 用 `abc.ABC` 而非 `typing.Protocol`，
      这样"忘了实现某个方法"会在**实例化时**就报错，而不是等到调用那一刻。
    """

    @abstractmethod
    async def search(
        self,
        collection: str,
        query: str,
        top_k: int = 20,
        filters: Optional[dict[str, Any]] = None,
    ) -> Sequence[SearchHit]:
        """检索并返回按相关度降序排列的命中项。

        `collection` 是**逻辑集合名**（如 `episodic`），不得是具体表名或索引名 ——
        换实现时集合语义不变。`query` 是查询文本，`top_k` 是返回上限，
        `filters` 是结构化过滤条件（如 `{"tenant_id": "default"}`）。

        ★ 无结果时返回**空序列**，不得返回 `None` ——
        否则调用方无法区分"查了没结果"与"没查成"。
        """
        raise NotImplementedError

    @abstractmethod
    async def index(self, collection: str, doc_id: str, text: str,
                    metadata: Optional[dict[str, Any]] = None) -> None:
        """写入/更新一条可检索文档（幂等：同 doc_id 覆盖）。"""
        raise NotImplementedError

    @abstractmethod
    async def health(self) -> tuple[bool, str]:
        """健康检查，返回 ``(是否可用, 说明)``。

        ★ 必须可区分"可用但无数据"与"不可用" —— 前者返回 ``(True, "ok")``。
        """
        raise NotImplementedError


# ==================================================================
#  2. TopologyStore（Postgres 表 → Neo4j）
# ==================================================================
class TopologyStore(ABC):
    """服务拓扑接口 —— 支撑依赖图、上下游与**爆炸半径**计算。

    ★ 命名刻意不含存储实现细节：
      工具名与接口名都用 `topology`，不用 `cypher`。
      这样从 Postgres 表切到 Neo4j 时，调用方零改动。
    """

    @abstractmethod
    async def upsert_service(
        self,
        service_id: str,
        name: str,
        *,
        env: str = "prod",
        cluster: Optional[str] = None,
        owner: Optional[str] = None,
        tier: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """写入或更新一个服务节点（幂等）。"""
        raise NotImplementedError

    @abstractmethod
    async def upsert_dependency(self, edge: DependencyEdge) -> None:
        """写入或更新一条依赖边（幂等）。"""
        raise NotImplementedError

    @abstractmethod
    async def get_dependencies(
        self, service_id: str, direction: str = "downstream", depth: int = 1
    ) -> Sequence[DependencyEdge]:
        """查询依赖关系，返回依赖边序列（**无结果返回空序列**）。

        `direction` 取 `"downstream"`（它调用谁）或 `"upstream"`（谁调用它）；
        `depth` 是跳数，`depth=1` 为直接邻居。
        """
        raise NotImplementedError

    @abstractmethod
    async def blast_radius(
        self, service_id: str, qps_estimates: Optional[dict[str, float]] = None
    ) -> dict[str, Any]:
        """计算某服务的**爆炸半径**（风险分级的关键输入）。

        ★ 注意 `qps_estimates` 由 **Metrics Agent** 查得后传入
          （拓扑侧拿不到 QPS，不能自己编）。
          本方法只负责算"受影响服务数/上游数"，QPS 仅作汇总入参。

        返回 `{"services_affected": int, "upstream_count": int, "qps_affected": float}`。
        """
        raise NotImplementedError

    @abstractmethod
    async def health(self) -> tuple[bool, str]:
        """健康检查，返回 ``(是否可用, 说明)``。"""
        raise NotImplementedError


# ==================================================================
#  3. ChangeChannel（应用层 → Argo CD）
# ==================================================================
class ChangeChannel(ABC):
    """变更通道 —— Agent 对集群的**唯一**写入口。

    ★★ 两条硬性约束：

    1. **Agent 任何代码不得直接调用集群写 API**，写操作必须走此通道。
    2. **`execute()` 内部必须重新校验等级与审批，不信任调用方传入的等级。**
       因此 `execute()` 的签名**刻意不接收等级参数** ——
       从类型层面就杜绝了调用方"传一个等级进来"的可能，
       这比在文档里写一句"请勿信任调用方"更可靠。
    """

    @abstractmethod
    async def propose(self, action: ProposedAction,
                      qps_estimates: Optional[dict[str, float]] = None) -> RiskDecision:
        """对提议的动作做风险分级，返回判定结果。

        Args:
            action: 待评估的变更动作。
            qps_estimates: QPS 估算（由 Metrics Agent 提供）。

        Returns:
            `RiskDecision`。调用方**应当**据此决定是自动执行、请求审批还是拒绝。
        """
        raise NotImplementedError

    @abstractmethod
    async def execute(self, action: ProposedAction,
                      approval: Optional[dict[str, Any]] = None) -> ChangeResult:
        """执行变更。

        ★ 实现必须在此方法内部**重新**做两件事，不得省略：
          ① 重新计算风险等级（不信任 propose 的结果，也不接受调用方传入）
          ② 校验审批令牌：
             - `action_hash` 必须与当前动作一致（防"批准 A 却做 B"）
             - 审批人必须以 `human:` 开头（不能是 Agent 自己）
             - 令牌未过期

        Args:
            action: 待执行的动作。
            approval: 审批令牌；**L3 动作缺失时必须拒绝执行**，
                而不是"默认放行"。

        Returns:
            `ChangeResult`。含执行阶段、验证结果与是否已回滚。
        """
        raise NotImplementedError

    @abstractmethod
    async def dry_run(self, action: ProposedAction) -> str:
        """干跑并返回 diff 预览（L1 能力）。

        Returns:
            人类可读的 diff 文本（空字符串表示无变化）。
        """
        raise NotImplementedError

    @abstractmethod
    async def health(self) -> tuple[bool, str]:
        """健康检查，返回 ``(是否可用, 说明)``。"""
        raise NotImplementedError
