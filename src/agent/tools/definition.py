"""`define_tool(...)` —— 工具的**一份声明**（参考 dsh 的 `defineTool`）。

--------------------------------------------------------------------------------
★ 目标是消灭"加一个工具要改四个文件"
--------------------------------------------------------------------------------

改前：`handlers.py` 写实现 → `summarize.py` 可能加裁剪器 → `registry.py` 写声明
→ `enums.py` 加进 `TOOL_NAMES` 并授权。四处里**任何一处忘记改**，症状都是"运行时静默失灵"。

改后：**一份声明**同时给出"给模型看的 schema"与"执行体"，外加**策略元数据**：

```python
@define_tool(
    name="latency_probe",
    description="读取某服务最近 5 分钟的 99 分位延迟。",
    parameters={...},                       # JSON Schema（给模型 + 第 1 道保护）
    capabilities=(CAP_METRICS,),            # ★ 授权按**能力**判，不按工具名硬编码
    risk_level="L0",
    granted_roles=("metrics_agent",),       # ★ 这只是**申请**，最终由策略裁定
    summarizer="summarize_series",
    rate_limit="30/min", timeout_sec=15,
)
async def latency_probe(ctx, params) -> RawOutcome: ...
```

--------------------------------------------------------------------------------
★ 三条刻意的约束
--------------------------------------------------------------------------------

1. **注册是申请，不是授权。** `granted_roles` 只表达"作者希望谁能用"；
   最终授权 = 能力集合 ∩ 角色能力 − 显式拒绝（`policy.py` 裁定）。
   这样即使有人写了个模块自称"给 remediation_agent 用"，也不会自动拿到写能力。
2. **高危能力必须显式开启**（写能力默认关闭，见 `policy.py`）。
3. **9 个契约工具名不可被扩展覆盖**，除非显式 `override=True` 且提升版本。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Final, Mapping


from ..common.contracts import ToolSpec
from ..common.enums import TOOL_NAMES, ToolPermission
from .summarize import get_summarizer
from .availability import Probe

__all__ = [
    "ToolDefinition",
    "define_tool",
    "DEFINITIONS_ATTR",
    "definitions_of",
    "CONTRACT_TOOL_NAMES",
]

#: 模块级"我声明了哪些工具"的约定属性名（加载器按它收）
DEFINITIONS_ATTR: Final[str] = "TOOL_DEFINITIONS"

#: 9 个**契约工具**（扩展不得冒用这些名字）。
#: ★★ **唯一来源是 `common/enums.TOOL_NAMES`**，这里只是它的别名引用：
#:   抄一份出来的后果是"加了第 10 个契约工具，而策略层那份没跟着更新"——
#:   于是扩展可以顶着新契约工具名注册进来（策略防的是"顶替契约工具"）。
CONTRACT_TOOL_NAMES: Final[tuple[str, ...]] = tuple(TOOL_NAMES)

# 导入期断言：契约工具名的**形态**必须立得住（名字重复会让"契约工具"名不副实）
assert len(CONTRACT_TOOL_NAMES) == len(set(CONTRACT_TOOL_NAMES)), (
    f"契约工具名有重复：{CONTRACT_TOOL_NAMES}")

Handler = Callable[[Any, Mapping[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class ToolDefinition:
    """一个工具的完整定义 = 声明（给模型/校验）+ 实现 + 策略元数据。"""

    spec: ToolSpec
    handler: Handler
    summarize: Callable[[Any], dict[str, Any]]

    # ---- 策略元数据（决定"谁能调、要不要审批"）----
    capabilities: tuple[str, ...] = ()
    risk_level: str = "L0"
    granted_roles: tuple[str, ...] = ()
    requires_approval: bool = False
    version: int = 1
    source: str = "builtin"                 # builtin / manifest:<path> / runtime
    origin_module: str = ""                 # 定义所在的模块（重载时用来注销旧定义）
    availability: Probe | None = None       # 配置预检；未知连通性仍由执行器验证
    verification_records: bool = False      # 返回具体验证记录，由黑板回查后采信

    # ---- 兼容旧名字 ----
    @property
    def name(self) -> str:
        return self.spec.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "permission": self.spec.permission.value,
            "capabilities": list(self.capabilities),
            "risk_level": self.risk_level,
            "granted_roles": list(self.granted_roles),
            "requires_approval": self.requires_approval,
            "source": self.source,
            "timeout_sec": self.spec.timeout_sec,
            "rate_limit": self.spec.rate_limit,
            "idempotent": self.spec.idempotent,
            "summarizer": self.spec.result_summarizer,
            "cost_weight": self.spec.cost_weight,
            "availability_probe": self.availability is not None,
            "verification_records": self.verification_records,
        }

    def with_metadata(self, **over: Any) -> "ToolDefinition":
        """派生一份改过元数据的定义（加载器盖章 `source` / `origin_module` 时用）。"""
        return replace(self, **over)


def define_tool(
    *,
    name: str,
    description: str,
    parameters: Mapping[str, Any],
    capabilities: tuple[str, ...] = (),
    risk_level: str = "L0",
    granted_roles: tuple[str, ...] = (),
    requires_approval: bool = False,
    version: int = 1,
    permission: ToolPermission = ToolPermission.READ_ONLY,
    timeout_sec: int = 30,
    rate_limit: str = "60/min",
    cost_weight: float = 1.0,
    idempotent: bool = True,
    summarizer: str = "summarize_records",
    source: str = "runtime",
    availability: Probe | None = None,
    verification_records: bool = False,
) -> Callable[[Handler], ToolDefinition]:
    """把 handler 变成一份 `ToolDefinition`（并登记进调用方模块的声明清单）。

    必填字段（name / description / parameters）缺一就在**定义期**抛 `ValueError`（fail-fast）。
    ★ 为什么要**登记进模块**：加载器（`loader.py`）要能"收走一个模块声明的全部工具"，
      才能在重载时注销**旧定义**（声明写在模块里，注册动作由挂载方执行）。
    """
    if not name or not name.strip():
        raise ValueError("define_tool: name 不能为空")
    if not description or not description.strip():
        raise ValueError(
            f"define_tool({name}): description 不能为空 —— 它是**给模型看的**，"
            f"决定模型会不会用对"
        )
    if not parameters:
        raise ValueError(
            f"define_tool({name}): parameters 不能为空 —— 第 1 道保护依赖它"
        )
    if risk_level not in ("L0", "L1", "L2", "L3", "L4"):
        raise ValueError(f"define_tool({name}): risk_level={risk_level!r} 不是 L0~L4")
    if version < 1:
        raise ValueError(f"define_tool({name}): version 必须 ≥ 1")

    def decorate(handler: Handler) -> ToolDefinition:
        spec = ToolSpec(
            name=name, description=description, input_schema=dict(parameters),
            permission=permission, timeout_sec=timeout_sec, rate_limit=rate_limit,
            cost_weight=cost_weight, idempotent=idempotent,
            result_summarizer=summarizer,
        )
        definition = ToolDefinition(
            spec=spec, handler=handler, summarize=get_summarizer(summarizer),
            capabilities=tuple(capabilities), risk_level=risk_level,
            granted_roles=tuple(granted_roles), requires_approval=requires_approval,
            version=version, source=source,
            origin_module=getattr(handler, "__module__", ""),
            availability=availability,
            verification_records=verification_records,
        )
        # ★ 登记到**定义所在模块**的 `TOOL_DEFINITIONS`：加载器据此"收走整包"。
        #   ★ 内置工具（`source="builtin"`）**不登记** —— 它们由 `registry.py` 显式装配，
        #     若也落进 `handlers` 模块的桶里，加载器一旦指向该模块就会把它们当扩展重装。
        if source != "builtin":
            import sys

            module = sys.modules.get(definition.origin_module)
            if module is not None:
                bucket = getattr(module, DEFINITIONS_ATTR, None)
                if bucket is None:
                    bucket = []
                    setattr(module, DEFINITIONS_ATTR, bucket)
                bucket.append(definition)
        return definition

    return decorate


def definitions_of(module: Any) -> list[ToolDefinition]:
    """取一个模块声明的全部工具（没有 `TOOL_DEFINITIONS` → 空列表，不报错）。"""
    raw = getattr(module, DEFINITIONS_ATTR, None)
    if not raw:
        return []
    out: list[ToolDefinition] = []
    for item in raw:
        if not isinstance(item, ToolDefinition):
            raise TypeError(
                f"{getattr(module, '__name__', module)}.{DEFINITIONS_ATTR} 里出现了"
                f"非 ToolDefinition 的条目：{type(item).__name__}"
            )
        out.append(item)
    return out
