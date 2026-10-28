"""1. `EntityRef`（实体对齐的锚点）。

★ 本模块的内容原样搬自原 `common/contracts.py`（它曾有 1090 行），
  **公开导入面一字未改** —— `from agent.common.contracts import …` 照旧可用
  （包的 `__init__.py` 显式再导出全部公开名）。
"""

from __future__ import annotations

from pydantic import Field, model_validator
from typing import Optional

from .base import ENTITY_ID_PREFIX, EntityType, PLACEHOLDER_CLUSTERS, _Contract


# ==================================================================
#  1. EntityRef
# ==================================================================
class EntityRef(_Contract):
    observed_id: Optional[str] = Field(default=None,pattern=r'^obs_[a-f0-9]{32}$')
    """实体引用 —— 全系统实体对齐的基础。

    `id` 为规范化形式 `type:name@cluster`，**全系统统一**。
    """

    type: EntityType
    id: str = Field(..., min_length=1, description="规范化 ID，如 svc:order@prod-cn-1")
    name: str = Field(..., min_length=1)
    namespace: Optional[str] = None
    cluster: Optional[str] = None

    @model_validator(mode="after")
    def _check_id_format(self) -> "EntityRef":
        """校验 id 符合 `type:name[@cluster]` 形式，且前缀与 type 自洽。

        ★ 为什么强制：Agent 定位错服务，后面所有取证全废。
          规范化 ID 是跨数据源对齐的唯一锚点。

        ★ 前缀用 ENTITY_ID_PREFIX 的缩写映射（svc/pod/db/cfg/...），
          与 `svc:order@prod-cn-1` 这类实际写法一致；
          生成端（make）与校验端共用同一映射，避免自相矛盾。

        ★★ **集群名允许缺失**：单集群部署、或后端没提供集群元数据时，
          `svc:order` 就是完整合法的 id。
          但**不允许占位值**（`@unknown`）—— 占位值一旦进库，
          "没拿到集群名"与"真有个叫 unknown 的集群"就再也分不开了。

        ★ 本方法曾有 54 行、8 个 `raise`。现在"拆 id + 逐条对账"
          搬进 `_validate_entity_id()`（纯函数，**不碰 pydantic**，可直接单测），
          这里只剩"调它、把错误转成 `ValueError`"。
        """
        _validate_entity_id(self.id, self.type.value, self.name, self.cluster)
        return self

    @classmethod
    def make(cls, entity_type: "EntityType | str", name: str,
             cluster: Optional[str] = None,
             namespace: Optional[str] = None) -> "EntityRef":
        """按规范构造（避免各处手拼 ID 出现格式偏差）。

        ★ `cluster` **可空**：单集群部署或后端没提供集群元数据时，
          id 就是 `svc:order`。
          ★ 空就是**空**，不要传占位值（`"unknown"` 会被校验器拒掉）。
        """
        et = entity_type if isinstance(entity_type, EntityType) else EntityType(entity_type)
        prefix = ENTITY_ID_PREFIX[et.value]
        clean_cluster = (cluster or "").strip() or None
        ident = (f"{prefix}:{name}@{clean_cluster}" if clean_cluster
                 else f"{prefix}:{name}")
        from ...observed.context import current
        scope=current()
        return cls(type=et, id=ident, name=name,observed_id=scope.observed_id if scope else None,
                   cluster=clean_cluster, namespace=namespace)


def _validate_entity_id(ident: str, entity_type: str, name: str,
                        cluster: Optional[str]) -> None:
    """`type:name[@cluster]` 的**逐条对账**（纯函数：给 id/字段，不合法就抛 `ValueError`）。

    ★ 从 `EntityRef._check_id_format`（54 行）里抽出来 ——
      它是**纯函数**，可以脱离 pydantic 直接单测；校验规则本身一行未改。

    检查项（顺序即报错优先级）：
      ① 必须有 `:`；② name 部分非空；③ `@` 后不能为空；④ 集群名不能是占位值；
      ⑤ 前缀与 `type` 自洽；⑥ id 里的 name 与 `name` 字段一致；
      ⑦ id 里的 cluster 与 `cluster` 字段一致（要么都写、要么都不写）。
    """
    if ":" not in ident:
        raise ValueError(f"EntityRef.id 必须为 `type:name[@cluster]` 形式，实际 {ident!r}")
    prefix, rest = ident.split(":", 1)
    name_part, at, cluster_part = rest.partition("@")
    if not name_part:
        raise ValueError(f"EntityRef.id 的 name 部分为空，实际 {ident!r}")
    if at and not cluster_part:
        raise ValueError(
            f"EntityRef.id 写了 `@` 但集群名为空（{ident!r}）—— "
            f"集群未知时应当**整个省略** `@cluster`，而不是留一个空的 `@`")
    if cluster_part.strip().lower() in PLACEHOLDER_CLUSTERS:
        raise ValueError(
            f"EntityRef.id 用了占位集群名（{cluster_part!r}）—— "
            f"集群未知时应当**省略** `@cluster`，"
            f"不得发明占位值：那会让「未知」与「真叫 {cluster_part} 的集群」无法区分")
    expected_prefix = ENTITY_ID_PREFIX.get(entity_type)
    if prefix != expected_prefix:
        raise ValueError(
            f"EntityRef.id 的前缀 {prefix!r} 与 type {entity_type!r} 不符 —— "
            f"应为 {expected_prefix!r}（见 ENTITY_ID_PREFIX）")
    if name != name_part:
        raise ValueError(
            f"EntityRef.id 中的 name {name_part!r} 与 name 字段 {name!r} 不一致")
    if cluster is not None and cluster_part and cluster != cluster_part:
        raise ValueError(
            f"EntityRef.id 中的 cluster {cluster_part!r} 与 cluster 字段 {cluster!r} 不一致")
    _check_cluster_consistency(ident, cluster_part, cluster)


def _check_cluster_consistency(ident: str, cluster_part: str,
                               cluster: Optional[str]) -> None:
    """`id` 里的集群与 `cluster` 字段**必须一致**（要么都写、要么都不写）。

    ★★ **两个方向都要查**。以前只查了"字段空 / id 有"这一个方向，
      于是 `EntityRef(id="svc:order", cluster="prod-cn-1")` 能通过 ——
      id 说"没有集群"、字段说"属于 prod-cn-1"，两个锚点各说各话。
      后果与占位集群名同族：Agent 会把"没集群的 svc:order"与"属于 prod-cn-1 的
      svc:order"当成同一个实体（或反过来），而 `id` 是跨数据源对齐的唯一锚点。
    ★ 为什么必须由校验器兜住：**集群名允许省略**之后，"id 没写集群"
      不再是一个必然的格式错误，只能靠这条一致性判据。
    """
    if cluster is None and cluster_part:
        # id 里有集群名，字段却是空的 → 两者不一致（可读性/可追溯性都靠这两个字段对齐）
        raise ValueError(
            f"EntityRef.id 里写了集群名 {cluster_part!r}，而 cluster 字段为空 —— "
            f"两者必须一致（要么都写，要么都不写）")
    if cluster is not None and not cluster_part:
        raise ValueError(
            f"EntityRef.cluster 有值（{cluster!r}）而 id {ident!r} 里没写集群 —— "
            f"两者必须一致（要么都写，要么都不写）；"
            f"请用 `EntityRef.make(type, name, cluster)` 构造，不要手拼 id")
