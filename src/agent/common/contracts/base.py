"""模型基类 + 契约自带的枚举/常量（`_Contract` / `EntityType` / `EnvName` / 前缀表）。

★ 本模块的内容原样搬自原 `common/contracts.py`（它曾有 1090 行），
  **公开导入面一字未改** —— `from agent.common.contracts import …` 照旧可用
  （包的 `__init__.py` 显式再导出全部公开名）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from typing import Final

from enum import Enum


# ==================================================================
#  模型基类
# ==================================================================
class _Contract(BaseModel):
    """所有契约对象的基类。

    ``protected_namespaces=()``：Pydantic v2 默认把 ``model_`` 前缀视为保留，
    而本项目有 ``model`` 语义的字段（如 ``router_model``），显式放开以免误报。
    """

    model_config = ConfigDict(
        extra="forbid",            # ★ 禁止未声明字段：契约必须严格，多余字段即违约
        validate_assignment=True,  # 赋值后也校验，避免"改了以后不再合法"
        protected_namespaces=(),
        str_strip_whitespace=True,
    )


# ==================================================================
#  附属枚举（出现在契约"约束"列的取值集合）
#
#  ★ EntityType 与 EnvName 定义在此（而非 enums.py），因为它们只服务
#    EntityRef / Slots 的取值约束，属契约对象的组成部分；
#    而 ActionClass / RiskLevel / ErrorCode 等**跨层**枚举集中在 enums.py。
# ==================================================================
class EntityType(str, Enum):
    """实体类型：service / pod / node / database / middleware / config。"""

    SERVICE = "service"
    POD = "pod"
    NODE = "node"
    DATABASE = "database"
    MIDDLEWARE = "middleware"
    CONFIG = "config"

    def __str__(self) -> str:
        return str(self.value)


class EnvName(str, Enum):
    """环境名：prod / staging / dev，默认 prod。"""

    PROD = "prod"
    STAGING = "staging"
    DEV = "dev"

    def __str__(self) -> str:
        return str(self.value)


# ★ EntityRef.id 的前缀映射
#
#  `EntityRef.id` 形如 `type:name@cluster`，但实际用的是缩写前缀
#  （如 `svc:order@prod-cn-1`），不是完整枚举值。
#  为避免"生成端写 svc、校验端要求 service"这类自相矛盾（本实现首版即如此，
#  被契约验收抓出），此处把映射显式固化，生成与校验共用同一来源。
ENTITY_ID_PREFIX: Final[dict[str, str]] = {
    EntityType.SERVICE.value: "svc",
    EntityType.POD.value: "pod",
    EntityType.NODE.value: "node",
    EntityType.DATABASE.value: "db",
    EntityType.MIDDLEWARE.value: "mw",
    EntityType.CONFIG.value: "cfg",
}

#: ★ 被明确禁止的"占位集群名"（集群未知时**省略** `@cluster`）。
#: 为什么禁止：占位值一旦进库，"没拿到集群名"与"真有个叫 unknown 的集群"
#: 就再也分不开了 —— 而实体对齐与记忆相关度都吃这个值。
#: ★★ **全系统唯一定义处**：`intent/slots.py` 与 `tests/test_internal_contracts.py`
#:   都从这里取（以前这里一份、slots.py 一份，`"default"` 只在一份里，
#:   于是同一个假的集群名在配置装载时被拒、在 `EntityRef.id` 里却被放行）。
PLACEHOLDER_CLUSTERS: Final[frozenset[str]] = frozenset(
    {"unknown", "none", "null", "n/a", "na", "-", "tbd", "default"})
