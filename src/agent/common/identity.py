"""部署身份的**唯一阶梯** —— "这次观测的集群/环境/命名空间到底是什么"。

--------------------------------------------------------------------------------
★ 为什么必须只有一个地方回答这个问题
--------------------------------------------------------------------------------

"集群名"是**部署事实**。它原先散在 5 个文件里、还有 2 个不同取值
（prometheus/promtail/tempo/otel-collector 各写一份 `compose-cn-1`，
SDK 又硬编码 `local-1`），Agent 侧则靠一个硬编码默认值猜 ——
换个部署名就会**静默打错标签**，没有任何地方会报错。

所以本模块把"从哪拿"这件事**收敛成一条阶梯**（顺序即权威性）：

  1. **用户明说**（"prod-cn-2 集群的 order"）—— 用户是当场最权威的信息源
  2. **被观测系统自报**（`observed_info.yaml` ← `GET /services`）—— 部署事实归被观测系统
  3. **Agent 侧配置/环境**（`AIOPS_CLUSTER`）—— 兜底：被观测系统还没升级到会自报的 SDK 时用
  4. （无）→ `None` —— ★ **不发明值**：空就是空

--------------------------------------------------------------------------------
★ 两条硬规则
--------------------------------------------------------------------------------

1. **空是合法结果**。单集群部署、后端没提供集群元数据时，"未知"就是正确答案；
   契约里 `EntityRef.id` 允许省略 `@cluster`，下游匹配也全部
   与集群名无关（服务名才是定位符）。
2. **不许发明占位值**（`@unknown`/`default`/`localhost` 之类）。
   发明出来的值会让"没拿到集群名"与"真的是这个集群"**再也分不开** ——
   而实体对齐、记忆相关度都吃这个值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from .config import ENV_CLUSTER, ENV_ENV, ENV_NAMESPACE

__all__ = [
    "Identity",
    "UNKNOWN",
    "user_identity",
    "env_identity",
    "observed_identity",
    "resolve",
    "ObservedServiceLike",
]

#: 来源标记（进日志与证据的原文里，便于回答"这个集群名是谁给的"）
UNKNOWN = "unknown"

#: 阶梯第 3 级读的环境变量名 —— ★ 键名取自 `config.py`（那里是唯一来源），
#: 这里只负责"字段 → 键"的对应关系。
_ENV_KEYS: Mapping[str, str] = {
    "cluster": ENV_CLUSTER,
    "env": ENV_ENV,
    "namespace": ENV_NAMESPACE,
}

#: 自报服务的形状。★ 刻意就是 `Any`，不做成 `Protocol`：调用方既可能传
#: `Mapping`（`observed_info.yaml` 解析出来的 dict），也可能传带属性的对象
#: （探活返回的 `SimpleNamespace`），两者都要能进 `observed_identity`，
#: 取值统一由 `_attr` 兼容。写成 `Protocol` 反而会把其中一半调用方挡在类型层面。
ObservedServiceLike = Any


@dataclass(frozen=True)
class Identity:
    """一次观测的部署身份（**三个字段都可空**）+ 每一项的来源。"""

    cluster: Optional[str] = None
    env: Optional[str] = None
    namespace: Optional[str] = None
    #: 字段 → 来源（`user` / `self_report` / `env` / `unknown`）
    sources: Mapping[str, str] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        return any((self.cluster, self.env, self.namespace))

    def source_of(self, field: str) -> str:
        return str((self.sources or {}).get(field) or UNKNOWN)

    def to_dict(self) -> dict[str, Any]:
        return {"cluster": self.cluster, "env": self.env, "namespace": self.namespace,
                "sources": dict(self.sources or {})}

    def render(self) -> str:
        parts = []
        for field in ("cluster", "env", "namespace"):
            value = getattr(self, field)
            parts.append(f"{field}={value or '（未提供）'}(来源 {self.source_of(field)})")
        return "；".join(parts)


# ======================================================================
#  阶梯的三级
# ======================================================================
def user_identity(cluster: Optional[str] = None, env: Optional[str] = None,
                  namespace: Optional[str] = None) -> dict[str, Optional[str]]:
    """第 1 级：用户当场说的（只有明确给了值的字段才算）。"""
    return {"cluster": _clean(cluster), "env": _clean(env), "namespace": _clean(namespace)}


def env_identity(env: Optional[Mapping[str, str]] = None) -> dict[str, Optional[str]]:
    """第 3 级：Agent 侧环境变量（`AIOPS_CLUSTER` / `AIOPS_ENV` / `AIOPS_NAMESPACE`）。

    ★ 这三个值的来源是 `deploy/.env`（唯一定义处），由 `run.ps1` 读进环境。
    """
    source = os.environ if env is None else env
    return {field: _clean(source.get(key)) for field, key in _ENV_KEYS.items()}


def observed_identity(services: Optional[Iterable[ObservedServiceLike]] = None
                      ) -> tuple[dict[str, Optional[str]], list[str]]:
    """第 2 级：被观测系统自报（`observed_info.yaml` 的每条服务）。

    返回 `(身份, 冲突说明)`。★ 自报之间**不一致**时不猜：只保留**一致**的字段，
    其余置空并把冲突写进说明（多集群/多环境时"全系统一个集群名"本就不成立）。
    """
    rows = [s for s in (services or ())]
    values: dict[str, Optional[str]] = {"cluster": None, "env": None, "namespace": None}
    conflicts: list[str] = []
    for field in ("cluster", "env", "namespace"):
        reported = {_clean(_attr(row, field)) for row in rows}
        reported.discard(None)
        if len(reported) == 1:
            values[field] = next(iter(reported))
        elif len(reported) > 1:
            conflicts.append(f"{field} 自报不一致：{sorted(reported)}（保留为空，不猜）")
    return values, conflicts


def resolve(*, user: Optional[Mapping[str, Optional[str]]] = None,
            observed: Optional[Iterable[ObservedServiceLike]] = None,
            env: Optional[Mapping[str, str]] = None,
            observed_conflicts: Optional[list[str]] = None) -> Identity:
    """按阶梯合成一份身份（**取到就停，取不到就空**）。"""
    obs_values, conflicts = observed_identity(observed)
    if observed_conflicts is not None:
        observed_conflicts.extend(conflicts)
    user_values = {key: _clean(value) for key, value in dict(user or {}).items()}
    env_values = env_identity(env)

    values: dict[str, Optional[str]] = {}
    sources: dict[str, str] = {}
    for field in ("cluster", "env", "namespace"):
        for source, table in (("user", user_values), ("self_report", obs_values),
                              ("env", env_values)):
            value = table.get(field)
            if value:
                values[field] = value
                sources[field] = source
                break
        else:
            values[field] = None
            sources[field] = UNKNOWN
    return Identity(cluster=values["cluster"], env=values["env"],
                    namespace=values["namespace"], sources=sources)


def _clean(value: Any) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text or None


def _attr(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)
