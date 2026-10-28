"""工具登记表 —— **10 个契约工具的唯一声明处** + **可动态扩展的注册表**。

--------------------------------------------------------------------------------
★ 从"模块级 dict"升级为**注册表服务**
--------------------------------------------------------------------------------

| 能力 | 实现 |
|---|---|
| 注册即可见 | `ToolRegistry.visible_to(role)` → `executor.llm_tools(role)` 直接派生（模型看到的 = 可调用的） |
| 动态注册 | `registry.register(definition)`，**注册期**过策略校验（能力/红线/写能力开关） |
| 注销与重载 | `unregister(name)`；加载器重载时"注销旧定义 → 注册新定义" |
| 可解释 | `revision`（每次变更 +1）+ `snapshot_hash()`（进调用审计：**老审计能对上当时那一版**） |
| 变更留痕 | 每次注册/注销写 `RegistryChange`（结构化日志 + 内存列表） |
| 作用域 | 授权按**能力**（`policy.py`）判定，不是按工具名硬编码 |

★ 契约工具的完整性仍然强制：`validate_registry()` 在导入期断言
  "契约工具名**一个不少**（名字取自 `enums.TOOL_NAMES`，不在这里手抄份数），
  且 `AGENT_TOOL_MATRIX` 的授权结果与能力模型**逐格一致**"。
  扩展工具只能**附加**，不能顶替。
"""

from __future__ import annotations

from typing import Any, Final, Mapping, Optional

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..common.enums import AGENT_TOOL_MATRIX, TOOL_NAME_SET, TOOL_NAMES, ToolPermission
from ..common.query import (
    DEFAULT_LOG_LIMIT,
    DEFAULT_SERIES_LIMIT,
    DEFAULT_TRACE_LIMIT,
    MAX_LOG_LIMIT,
    MAX_SERIES_LIMIT,
    MAX_TRACE_LIMIT,
)
from ..intent.slots import ACTIVE_SLOTS
from . import fields as F
from . import summarize as S
from .definition import ToolDefinition, define_tool
from .handlers import HANDLERS
from .math_tools import MATH_SCHEMAS, MATH_TOOL_NAMES

#: ★★ 工具 schema 里的 `window` 默认值 —— **只从配置取这一处**
#:   （`intent/slots.yaml::defaults.time_window`）。修前四个 schema 各硬编码一份。
DEFAULT_WINDOW: Final[str] = str(ACTIVE_SLOTS.default_time_window)

#: ★★ 三个查询工具 `description` 里**必须**写全的两条取数局限（文案与数字都只有
#:   这一处来源）：模型是照着 `description` 决定"要不要再查一次"的 ——
#:   没写清"你看到的只是最近一段明细"，它就会把「没看到」说成「没发生」。
_LIMITS_SENTENCE: Final[str] = (
    "★ 取数局限（必须知道）：" + F.TIME_LIMITS.removeprefix("★ 取数局限：")
)
from .policy import (
    CAP_CHANGE,
    CAP_EVIDENCE_LOOKUP,
    CAP_KNOWLEDGE,
    CAP_LOGS,
    CAP_METRICS,
    CAP_MEMORY,
    CAP_RAW_TELEMETRY,
    CAP_TOPOLOGY,
    CAP_TRACES,
    CAP_WRITE,
    RegistrationDecision,
    ToolPolicy,
)
from .rate_limit import parse_rate_limit

__all__ = [
    "ToolBinding",
    "ToolRegistry",
    "RegistryChange",
    "RegistrationError",
    "BUILTIN_REGISTRY",
    "TOOL_REGISTRY",
    "build_builtin_registry",
    "build_default_registry",
    "validate_registry",
    "registry_summary",
    "tool_policy_default",
    "allow_write_from_env",
    "ENV_ALLOW_WRITE",
]

logger = logging.getLogger("agent.tools.registry")

#: 旧名字：`ToolBinding` 就是 `ToolDefinition`（保持既有调用方与测试可用）
ToolBinding = ToolDefinition


class RegistrationError(ValueError):
    """注册被策略拒绝（或登记表自身不一致）—— 调用方必须**拒绝注册**，不得降级放行。"""


# ======================================================================
#  变更留痕
# ======================================================================
@dataclass(frozen=True)
class RegistryChange:
    """一次注册表变更（谁、什么时候、加了/删了什么、实际授权给谁）。"""

    action: str                 # register / unregister / reload
    tool: str
    version: int = 0
    source: str = ""
    effective_roles: tuple[str, ...] = ()
    revoked: tuple[str, ...] = ()
    revision: int = 0
    actor: str = "system"
    reason: str = ""
    occurred_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "occurred_at": self.occurred_at, "actor": self.actor,
            "action": self.action, "tool": self.tool, "version": self.version,
            "source": self.source, "effective_roles": list(self.effective_roles),
            "revoked": list(self.revoked), "revision": self.revision,
            "reason": self.reason,
        }


# ======================================================================
#  注册表
# ======================================================================
@dataclass
class ToolRegistry:
    """工具注册表。**注册即可见**，但注册要过策略（`ToolPolicy`）。"""

    policy: ToolPolicy = field(default_factory=ToolPolicy)
    changes: list[RegistryChange] = field(default_factory=list)
    _tools: dict[str, ToolDefinition] = field(default_factory=dict, repr=False)
    _revision: int = 0

    # ------------------------------------------------------------------
    @property
    def revision(self) -> int:
        return self._revision

    def snapshot_hash(self) -> str:
        """当前注册表的指纹（名字 + 版本 + schema）—— 写进调用审计，老审计可解释。

        ★ 为什么用"名字:版本:schema 哈希"而不是对象哈希：进程重启后对象地址会变，
          而这份指纹是**稳定**的（同样的工具集合 → 同样的值）。
        """
        items = sorted(
            f"{name}:{definition.version}:"
            f"{hashlib.sha256(json.dumps(definition.spec.input_schema, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]}"
            for name, definition in self._tools.items()
        )
        return hashlib.sha256("|".join(items).encode()).hexdigest()[:16]

    # ------------------------------------------------------------------
    def get(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def all(self) -> dict[str, ToolDefinition]:
        """全部工具（**副本**：调用方改不动注册表）。"""
        return dict(self._tools)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    # ------------------------------------------------------------------
    def register(
        self, definition: ToolDefinition, *, actor: str = "system",
        reason: str = "", override: bool = False,
    ) -> RegistrationDecision:
        """注册一个工具。**注册期**就过策略校验（高危控制在最前面）。

        Args:
            override: 允许覆盖同名工具（用于"内置工具升级"：名字不变、`version` 提升）。
                ★ 默认为 False —— 静默覆盖是"悄悄换掉一个已授权的工具"，不可接受。

        Raises:
            RegistrationError: 策略拒绝或覆盖非法。**不返回"半成功"**。
        """
        existing = tuple(self._tools)
        decision = self.policy.validate_registration(definition, existing=existing)

        if not decision.allowed and override and definition.name in self._tools:
            # 覆盖场景：同名不算冲突，其余判定（能力/红线/写能力）仍必须通过
            decision = self.policy.validate_registration(
                definition, existing=tuple(n for n in existing if n != definition.name))

        if not decision.allowed:
            raise RegistrationError(f"拒绝注册 {definition.name}：{decision.reason}")

        previous = self._tools.get(definition.name)
        # ★ 这里**没有**"同名 + 未 override"分支：那种情况已经在上面
        #   `validate_registration(existing=...)` 里被拒（"已经注册过了（不得静默覆盖）"），
        #   再写一条永远不会被看到的报错只是死代码（而且它的措辞更详细，会让人
        #   以为"不得静默覆盖"是在这里守的）。
        if previous is not None and override and definition.version <= previous.version:
            raise RegistrationError(
                f"{definition.name} 覆盖失败：新 version={definition.version} "
                f"必须大于旧 version={previous.version}"
            )

        self._tools[definition.name] = definition
        self._revision += 1
        self._log_change(RegistryChange(
            action="register", tool=definition.name, version=definition.version,
            source=definition.source, effective_roles=decision.effective_roles,
            revoked=decision.revoked, revision=self._revision, actor=actor,
            reason=reason or ("覆盖旧版本" if previous else ""),
        ))
        if decision.revoked:
            # ★ 申请了但没拿到 —— 必须说出来，否则作者会以为"注册成功=授权成功"
            logger.warning("工具 %s 注册成功，但以下申请被能力策略压掉：%s",
                           definition.name, list(decision.revoked))
        return decision

    def unregister(self, name: str, *, actor: str = "system", reason: str = "") -> bool:
        if name not in self._tools:
            return False
        definition = self._tools.pop(name)
        self._revision += 1
        self._log_change(RegistryChange(
            action="unregister", tool=name, version=definition.version,
            source=definition.source, revision=self._revision, actor=actor, reason=reason,
        ))
        return True

    def unregister_module(self, module_name: str, *, actor: str = "system",
                          reason: str = "reload") -> tuple[str, ...]:
        """注销某个模块声明的全部工具（重载时先清旧定义）。"""
        victims = [name for name, definition in self._tools.items()
                   if definition.origin_module == module_name]
        for name in victims:
            self.unregister(name, actor=actor, reason=reason)
        return tuple(victims)

    # ------------------------------------------------------------------
    def visible_to(self, role: str) -> list[ToolDefinition]:
        """该角色**看得到**的工具（模型看到的 = 可调用的）。"""
        return [definition for name, definition in sorted(self._tools.items())
                if self.policy.can_call(role, definition).allowed]

    def _log_change(self, change: RegistryChange) -> None:
        self.changes.append(change)
        logger.info("registry %s", json.dumps(change.to_dict(), ensure_ascii=False))


# ======================================================================
#  内置 10 个契约工具
# ======================================================================
#: 内置工具要求的**能力**（授权按它算；`AGENT_TOOL_MATRIX` 是授权结果）
_BUILTIN_CAPABILITIES: Final[dict[str, tuple[str, ...]]] = {
    "promql_read": (CAP_RAW_TELEMETRY, CAP_METRICS),
    "logql_read": (CAP_RAW_TELEMETRY, CAP_LOGS),
    "traceql_read": (CAP_RAW_TELEMETRY, CAP_TRACES),
    # ★ 下钻工具与 `traceql_read` **同一组能力**：它读的是同一批 trace（只是换个粒度）。
    #   给它单独一族能力，等于声称"能读链路的人不一定能读链路里的某一条" —— 没有意义。
    "trace_read": (CAP_RAW_TELEMETRY, CAP_TRACES),
    # ★★ 「谁能读遥测，谁就能读它的原始数据」：原始副本是**同一批数据**的另一面。
    #   与 `promql_read` **同一组能力**（也因此同一批角色）——按这条口径，谁能调
    #   `promql_read` 谁就能回取那份数据的原文，反之亦然，判定表逐格对得上。
    #   ★ 要放开给日志/链路两路，得先想清楚"哪一族的原文归谁看"（那是一次权限变更）。
    "raw_read": (CAP_RAW_TELEMETRY, CAP_METRICS),
    # ★★ 数据运算工具（契约修订 21）：算的是**同一批指标数据**，所以与 `raw_read`
    #   同一组能力 —— "能读这份遥测的人才能算它"，判定表逐格对得上（不新增能力族）。
    **{name: (CAP_RAW_TELEMETRY, CAP_METRICS) for name in MATH_TOOL_NAMES},
    "change_read": (CAP_CHANGE,),
    "topology_read": (CAP_TOPOLOGY,),
    "memory_read": (CAP_MEMORY,),
    "evidence_lookup": (CAP_EVIDENCE_LOOKUP,),
    "kb_search": (CAP_KNOWLEDGE,),
    "kb_document_read": (CAP_KNOWLEDGE,),
    "change_write": (CAP_WRITE,),
}

#: 内置工具的**风险下限**（真实等级由 `action_class` 在调用期决定，见 `DEFAULT_ACTION_LEVEL`）
_BUILTIN_RISK: Final[dict[str, str]] = {
    "change_write": "L2",          # L2 = 低危可回滚，自动执行 + 事后审计
}

_PROMQL_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "operation": {"type":"string","enum":["series","catalog"],"description":"series读取时序；catalog按登记服务选择器读取版本化指标目录，明确原名、类型、单位未知与覆盖，不返回指标采样或健康结论"},
        "cursor": {"type":"string","maxLength":512,"description":"仅catalog：上一页返回的作用域/窗口/快照绑定游标"},
        "query": {"type": "string", "minLength": 1,
                  "description": "PromQL 查询。★ 必须带具体 label 过滤（如 "
                                 "`db_pool_waiting_connections{service=\"order\"}`），"
                                 "不允许 `rate(x_total[5m])` 这类全量扫描"},
        "start": {"type": "string", "description": "ISO8601 或 epoch 秒（与 end 成对）。"
                                                   + F.TIME_LIMITS},
        "end": {"type": "string", "description": "ISO8601 或 epoch 秒（与 start 成对）。"
                                                 + F.TIME_LIMITS},
        # ★★ 默认值**只从配置取一处**（`intent/slots.yaml::defaults.time_window`），
        #   上限 7d —— 修前这里硬编码 `30m`，与配置各写一份。
        #   ★ 局限文案（窗口 ≤ 7d / 明细只召回最近 12.5h / step 恒 30s）从
        #   `common/query.py` 的常量现算：数字一变，模型看到的话跟着变。
        "window": {"type": "string", "default": DEFAULT_WINDOW,
                   "description": f"（默认 {DEFAULT_WINDOW}）" + F.RANGE_DESCRIPTION},
        "step": {"type": "string", "default": "30s", "description": "采样步长（如 30s / 1m）"},
        # ★ 这个参数修前**没有传到取数端**（声明了却不生效）→ 现在真的生效
        "limit": {"type": "integer", "default": DEFAULT_SERIES_LIMIT,
                  "minimum": 1, "maximum": MAX_SERIES_LIMIT,
                  "description": F.limit_description(
                      "**时间序列**", default=DEFAULT_SERIES_LIMIT,
                      maximum=MAX_SERIES_LIMIT)},
    },
    "required": ["query"],
    "additionalProperties": False,
}

_LOGQL_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1,
                  "description": "LogQL 查询。★ 必须带流选择器，如 "
                                 "`{service=\"order\"} | json | level=\"ERROR\"`"},
        "start": {"type": "string", "description": "ISO8601 或 epoch 秒（与 end 成对）。"
                                                   + F.TIME_LIMITS},
        "end": {"type": "string", "description": "ISO8601 或 epoch 秒（与 start 成对）。"
                                                 + F.TIME_LIMITS},
        "window": {"type": "string", "default": DEFAULT_WINDOW,
                   "description": f"（默认 {DEFAULT_WINDOW}）" + F.RANGE_DESCRIPTION},
        "limit": {"type": "integer", "default": DEFAULT_LOG_LIMIT,
                  "minimum": 1, "maximum": MAX_LOG_LIMIT,
                  "description": F.limit_description(
                      "行**日志**", default=DEFAULT_LOG_LIMIT, maximum=MAX_LOG_LIMIT)},
        "direction": {"type": "string", "enum": ["backward", "forward"], "default": "backward",
                      "description": "backward = 从新到旧（默认；★ 配合 limit 就是"
                                     "「只取最近 N 行」，更早的日志**不会**被取回）"},
    },
    "required": ["query"],
    "additionalProperties": False,
}

_TRACEQL_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1,
                  "description": "TraceQL 查询。★ 必须带服务/span 过滤，如 "
                                 "`{ resource.service.name = \"order\" && duration > 500ms }`"},
        "start": {"type": "string", "description": "ISO8601 或 epoch 秒（与 end 成对）。"
                                                   + F.TIME_LIMITS},
        "end": {"type": "string", "description": "ISO8601 或 epoch 秒（与 start 成对）。"
                                                 + F.TIME_LIMITS},
        "window": {"type": "string", "default": DEFAULT_WINDOW,
                   "description": f"（默认 {DEFAULT_WINDOW}）" + F.RANGE_DESCRIPTION},
        "limit": {"type": "integer", "default": DEFAULT_TRACE_LIMIT,
                  "minimum": 1, "maximum": MAX_TRACE_LIMIT,
                  "description": F.limit_description(
                      "条 trace", default=DEFAULT_TRACE_LIMIT,
                      maximum=MAX_TRACE_LIMIT)},
    },
    "required": ["query"],
    "additionalProperties": False,
}

#: `raw_read` 的入参 schema（`fields.py` 里声明：只读本地副本，**没有**重发上游的开关）
_RAW_READ_SCHEMA: Final[dict[str, Any]] = F.RAW_READ_SCHEMA

#: `trace_read` 的入参 schema（**只需要 trace_id**：窗口与过滤条件都由 id 决定）。
_TRACE_READ_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "trace_id": {"type": "string", "minLength": 1,
                     "description": "要下钻的那条 trace 的 id（32 位十六进制或 Tempo 的写法）。"
                                    "★ 从日志聚类证据的 tags 里取（`trace_id=…`），不要自己编。"
                                    "本工具按 id **精确取一条**，不做窗口扫描。"},
    },
    "required": ["trace_id"],
    "additionalProperties": False,
}

_CHANGE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "service": {"type": "string", "description": "服务名（如 order）；"
                                                     "或直接给规范化的 entity_id"},
        "change_id": {"type": "string", "description": "变更单号（如 CHG-1024）"},
        "cluster": {"type": "string", "description": "集群名（拼 entity_id 用）"},
        "start": {"type": "string", "description": "ISO8601 或 epoch 秒（与 end 成对）"},
        "end": {"type": "string", "description": "ISO8601 或 epoch 秒（与 start 成对）"},
        "window": {"type": "string", "default": DEFAULT_WINDOW,
                   "description": f"相对当前的时间窗（最长 7d）"},
        "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
    },
    "required": [],
    "additionalProperties": False,
}

_TOPOLOGY_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "service": {"type": "string", "minLength": 1,
                    "description": "服务名（如 order）或规范化的 entity_id"},
        "cluster": {"type": "string", "description": "集群名（拼 entity_id 用）"},
        "direction": {"type": "string", "enum": ["upstream", "downstream", "both"],
                      "default": "downstream",
                      "description": "downstream = 它依赖谁；upstream = 谁依赖它（爆炸半径）"},
        "depth": {"type": "integer", "default": 1, "minimum": 1, "maximum": 3,
                  "description": "跳数（最多 3；更大范围请分多次查）"},
    },
    "required": ["service"],
    "additionalProperties": False,
}

_MEMORY_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "symptom": {"type": "string", "minLength": 1,
                    "description": "现象描述（用来做相似案例召回）"},
        "service": {"type": "string", "description": "相关服务（可选，用于结构化过滤）"},
        "top_k": {"type": "integer", "default": 3, "minimum": 1, "maximum": 10},
    },
    "required": ["symptom"],
    "additionalProperties": False,
}

_EVIDENCE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "evidence_id": {"type": "string", "minLength": 1,
                        "description": "黑板上**已存在**的证据 id（如 ev_01J8…）。"
                                       "★ 只能反查，不能新增"},
    },
    "required": ["evidence_id"],
    "additionalProperties": False,
}

_KB_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength":4000, "description": "原始问题"},
        "rewritten_query": {"type":"string","maxLength":6000},
        "filters":{"type":"object","properties":{
            "category":{"type":"string","maxLength":80},
            **{k:{"type":"array","maxItems":60,"items":{"type":"string","maxLength":160}}
               for k in ('entities','exact_entities','dates','exact_codes','keywords')}},"additionalProperties":False},
        "top_k": {"type": "integer", "default": 10, "minimum": 1, "maximum": 10},
    },
    "required": ["query"],
    "additionalProperties": False,
}

_KB_DOCUMENT_SCHEMA = {"type":"object","properties":{"references":{"type":"array","minItems":1,"maxItems":10,
    "items":{"type":"object","properties":{"document_id":{"type":"string","pattern":"^kb_[a-f0-9]{32}$"},
    "version":{"type":"integer","minimum":1},"parent_id":{"type":"string","pattern":"^[a-f0-9]{64}$"}},
    "required":["document_id","version","parent_id"],"additionalProperties":False}}},
    "required":["references"],"additionalProperties":False}

_CHANGE_WRITE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "action_class": {"type": "string",
                         "enum": ["read", "dry_run", "restart_pod", "rollback_config",
                                  "drain_instance", "scale", "patch_route",
                                  "patch_resource_limit", "cross_namespace", "data_operation",
                                  "delete_namespace", "modify_rbac", "modify_network_policy",
                                  "control_plane", "delete_pvc"],
                         "description": "动作类别（决定起点风险等级，见 DEFAULT_ACTION_LEVEL）"},
        "target": {"type": "string", "minLength": 1,
                   "description": "目标实体（规范化的 entity_id，如 svc:order@compose-cn-1）"},
        "params": {"type": "object", "description": "动作参数（必须纯 JSON 可序列化）",
                   "additionalProperties": True},
        "dry_run": {"type": "boolean", "default": True,
                    "description": "★ 默认 true（只出 diff 不动手）；"
                                   "置 false 需要 L2 以上且必须走审批"},
        "reason": {"type": "string", "description": "为什么要做这个变更（进审计）"},
    },
    "required": ["action_class", "target"],
    "additionalProperties": False,
}


def _builtin(name: str, description: str, parameters: dict[str, Any], *,
             permission: ToolPermission = ToolPermission.READ_ONLY,
             timeout_sec: int = 30, rate_limit: str = "60/min",
             cost_weight: float = 1.0, idempotent: bool = True) -> Any:
    """声明一个内置工具（handler 取自 `handlers.HANDLERS`，**实现与声明仍分离**）。"""
    return define_tool(
        name=name, description=description, parameters=parameters,
        capabilities=_BUILTIN_CAPABILITIES[name],
        risk_level=_BUILTIN_RISK.get(name, "L0"),
        granted_roles=tuple(role for role, tools in AGENT_TOOL_MATRIX.items() if name in tools),
        permission=permission, timeout_sec=timeout_sec, rate_limit=rate_limit,
        cost_weight=cost_weight, idempotent=idempotent,
        summarizer=_BUILTIN_SUMMARIZERS[name], source="builtin",
    )(HANDLERS[name])


_BUILTIN_SUMMARIZERS: Final[dict[str, str]] = {
    "promql_read": "summarize_series",
    "logql_read": "summarize_logs",
    "traceql_read": "summarize_traces",
    "trace_read": "summarize_raw_trace",
    "raw_read": "summarize_raw",
    "change_read": "summarize_changes",
    "topology_read": "summarize_topology",
    "memory_read": "summarize_memory",
    "evidence_lookup": "summarize_evidence",
    "kb_search": "summarize_records",
    "kb_document_read": "summarize_records",
    "change_write": "summarize_records",
    # ★ 运算工具的产物是"算出来的数"，用同一个通用摘要器（保形 + 有界）
    **{name: "summarize_math" for name in MATH_TOOL_NAMES},
}

#: 数据运算工具的说明（**注入提示词的就是这些句子**）—— 每条都要说清
#: "只读本地副本、不发上游请求、算不了就报算不了"。
_MATH_DESCRIPTIONS: Final[dict[str, str]] = {
    "series_stats": "对已取回的序列做统计：个数/最小/最大/均值/中位/首值/末值/标准差。"
                    "★ 只读本地副本（不重发上游请求）；引用找不到就报「找不到」，不会给你 0。",
    "series_rate": "把累计型序列换算成**每秒速率**（含窗口内增量）—— 问「每秒多少」用这个。",
    "series_change": "末值相对首值的变化：绝对差 / 百分比 / 方向（涨/跌/持平）。",
    "series_slope": "线性趋势：斜率（每秒变化）与拟合优度 R² —— 问「趋势如何」用这个。",
    "series_compare": "两条序列对比（同环比）：末值差与百分比变化。",
    "math_eval": "白名单算术：weighted_mean（加权平均）/ sum / mean / ratio / pct_change。"
                 "★ 数字必须来自其他工具的结果，不要自己心算。",
}

_BUILTIN_DESCRIPTIONS: Final[dict[str, str]] = {
    "promql_read": "查询 Prometheus 指标（PromQL）。用于看现象、拐点、基线与容量。"
                   "★ 查询必须带 label 过滤（如 service=\"order\"）。" + _LIMITS_SENTENCE,
    "logql_read": "查询 Loki 日志（LogQL）。用于错误模式、首现时间与频次。"
                  "★ 必须带流选择器（如 {service=\"order\"}）。" + _LIMITS_SENTENCE
                  + "。" + F.limit_description("行日志", default=DEFAULT_LOG_LIMIT,
                                               maximum=MAX_LOG_LIMIT),
    "traceql_read": "查询 Tempo 链路（TraceQL）。用于耗时归因与 client/server span 对比。"
                    "★ 必须带服务或 span 过滤。" + _LIMITS_SENTENCE
                    + "。" + F.limit_description("条 trace", default=DEFAULT_TRACE_LIMIT,
                                                 maximum=MAX_TRACE_LIMIT),
    # ★ 这条说明必须让模型明白：它是**按 id 精确取一条**，不是又一次窗口扫描 ——
    #   否则模型会拿它当"再查一遍链路"用，把一次下钻变成一次全量查询。
    "trace_read": "按 `trace_id` 取回**整条 trace 的 span 树**（下钻到「断在哪一跳」）。"
                  "★ 只认 `trace_id`（从日志聚类的证据 tags 里拿），不做窗口扫描；"
                  "取不到就报「没查成」，不会给你一条空链路。",
    "change_read": "读取变更记录（`change_events` 表）。用于回答『是不是刚改了什么』。"
                   "至少要给 service 或 change_id。",
    "topology_read": "查询服务依赖拓扑（上下游、爆炸半径）。",
    # ★ 这条说明必须让模型明白"给你的是**本地副本**、不是重新查的上游"：
    #   老窗口的副本 ≠ 当前状态（把它说成"刚刚查到的"就是最坏的一种误导）。
    "raw_read": "取回**本地磁盘上的原始数据副本**（按 `raw_ref`）。用在摘要不够、"
                "要看原文时。★ 只读本地（**不重发上游请求**）、写明抓取时间；"
                "本地没有就明确报「找不到」，不会替你重新查一次。",
    "memory_read": "检索历史相似案例（记忆三路召回）。返回**必须带元数据**"
                   "（时间/验证方式/命中次数/是否被取代）。",
    "evidence_lookup": "反查黑板上**已有**的证据（仅 Critic 可用）。"
                       "★ 只能查已存在的 evidence_id：既不能新增假设，也不能读原始遥测。",
    "kb_search": "知识库原始/改写问题混合检索、两阶段RRF最多10块；只读当前范围生效知识，知识不是实时故障证据。",
    "kb_document_read": "按已召回原文id/版本/父块id去重读取知识原文，再次核查当前生效状态与系统归属。",
    **{name: _MATH_DESCRIPTIONS[name] for name in MATH_TOOL_NAMES},
    "change_write": "执行变更动作（重启/回滚/扩容…）。★ 这是**唯一**的写工具，"
                    "只由 Remediation Agent 持有，且必须经沙箱与分级审批。",
}

_BUILTIN_TOOLS: Final[tuple[ToolDefinition, ...]] = (
    _builtin("promql_read", _BUILTIN_DESCRIPTIONS["promql_read"], _PROMQL_SCHEMA),
    _builtin("logql_read", _BUILTIN_DESCRIPTIONS["logql_read"], _LOGQL_SCHEMA,
             cost_weight=1.2, rate_limit="30/min"),
    _builtin("traceql_read", _BUILTIN_DESCRIPTIONS["traceql_read"], _TRACEQL_SCHEMA,
             cost_weight=1.5, rate_limit="30/min"),
    # ★ 下钻：一条 trace 而已（不是窗口扫描）→ 成本低于 `traceql_read`，超时更短。
    _builtin("trace_read", _BUILTIN_DESCRIPTIONS["trace_read"], _TRACE_READ_SCHEMA,
             timeout_sec=20, cost_weight=0.8, rate_limit="60/min"),
    _builtin("raw_read", _BUILTIN_DESCRIPTIONS["raw_read"], _RAW_READ_SCHEMA,
             timeout_sec=10, cost_weight=0.3, rate_limit="120/min"),
    # ★★ 数据运算工具（契约修订 21）：**纯本地**（只读落盘的原始副本），因此超时短、
    #   幂等、成本低 —— 它们不该成为"再一次取数"。
    *[_builtin(name, _BUILTIN_DESCRIPTIONS[name], MATH_SCHEMAS[name],
               timeout_sec=5, cost_weight=0.1, rate_limit="600/min")
      for name in MATH_TOOL_NAMES],
    _builtin("change_read", _BUILTIN_DESCRIPTIONS["change_read"], _CHANGE_SCHEMA,
             timeout_sec=15, cost_weight=0.8),
    _builtin("topology_read", _BUILTIN_DESCRIPTIONS["topology_read"], _TOPOLOGY_SCHEMA,
             timeout_sec=10, cost_weight=0.5),
    _builtin("memory_read", _BUILTIN_DESCRIPTIONS["memory_read"], _MEMORY_SCHEMA,
             timeout_sec=10, cost_weight=0.6),
    _builtin("evidence_lookup", _BUILTIN_DESCRIPTIONS["evidence_lookup"], _EVIDENCE_SCHEMA,
             timeout_sec=5, cost_weight=0.2, rate_limit="120/min"),
    _builtin("kb_search", _BUILTIN_DESCRIPTIONS["kb_search"], _KB_SCHEMA,
             timeout_sec=30, cost_weight=0.8, rate_limit="30/min"),
    _builtin("kb_document_read", _BUILTIN_DESCRIPTIONS["kb_document_read"], _KB_DOCUMENT_SCHEMA,
             timeout_sec=10, cost_weight=0.3, rate_limit="60/min"),
    _builtin("change_write", _BUILTIN_DESCRIPTIONS["change_write"], _CHANGE_WRITE_SCHEMA,
             permission=ToolPermission.WRITE, timeout_sec=60, rate_limit="5/min",
             cost_weight=3.0, idempotent=False),
)


def tool_policy_default(*, allow_write: bool = False) -> ToolPolicy:
    """默认策略（写能力默认**关闭** —— 这个默认值本身就是一条安全控制）。"""
    return ToolPolicy(allow_write=allow_write)


#: 写能力开关的环境变量。★ 默认 **关闭**：忘了配的后果是"更安全"，不是"更危险"。
ENV_ALLOW_WRITE: Final[str] = "AIOPS_ALLOW_WRITE_TOOLS"
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


def allow_write_from_env(environ: Optional[Mapping[str, str]] = None) -> bool:
    """从环境变量读"是否允许写工具"（只认少数几个肯定写法，其余一律当**关闭**）。

    ★ 为什么不用 `bool(os.environ.get(...))`：那样 `AIOPS_ALLOW_WRITE_TOOLS=false`
      会变成 **True**（非空字符串）—— 一个"写着 false 却打开了写能力"的坑。
    """
    import os

    env = os.environ if environ is None else environ
    raw = str(env.get(ENV_ALLOW_WRITE) or "").strip().lower()
    return raw in _TRUTHY


def build_builtin_registry(*, allow_write: bool = False) -> ToolRegistry:
    """建一个只含内置 9 个契约工具的注册表。"""
    registry = ToolRegistry(policy=tool_policy_default(allow_write=allow_write))
    for definition in _BUILTIN_TOOLS:
        registry.register(definition, actor="system", reason="内置契约工具")
    return registry


def build_default_registry(
    *, allow_write: Optional[bool] = None, load_extensions: bool = True,
    manifest_path: Any = None, actor: str = "system",
) -> ToolRegistry:
    """应用启动时的标准装配：内置 9 个 + （可选）manifest 里启用的扩展。

    ★ `allow_write=None` → 读环境变量 `AIOPS_ALLOW_WRITE_TOOLS`（默认关闭）。
    ★ 扩展加载失败**不影响**内置工具（`loader.py` 会如实记录错误）。
    """
    if allow_write is None:
        allow_write = allow_write_from_env()
    registry = build_builtin_registry(allow_write=allow_write)
    if load_extensions:
        from .loader import ToolLoader  # 局部导入：避免 registry ↔ loader 循环

        for outcome in ToolLoader(registry, manifest_path=manifest_path).load(actor=actor):
            if outcome.errors:
                logger.warning("扩展加载有问题：%s", outcome.to_dict())
    from ..diagnostics.wiring import enabled
    if enabled():
        from ..diagnostics.wiring import register_tools
        register_tools(registry)
    from ..observed.sdk_access import register_tools as register_sdk
    register_sdk(registry)
    from ..observed.access_agent import register_tools as register_access
    register_access(registry)
    from ..webprobe.tools import register as register_web_probe
    register_web_probe(registry)
    return registry


#: 内置注册表（写能力关闭）；扩展由 `loader.py` 挂到它上面（或自己的实例上）
BUILTIN_REGISTRY: Final[ToolRegistry] = build_builtin_registry()

#: 兼容视图：`TOOL_REGISTRY[name] -> ToolDefinition`
TOOL_REGISTRY: Final[dict[str, ToolDefinition]] = BUILTIN_REGISTRY.all()


# ======================================================================
#  导入即校验
# ======================================================================
def validate_registry(registry: Mapping[str, ToolDefinition] | ToolRegistry | None = None) -> None:
    """校验登记表的不变量（只读工具与写工具分开声明）。**任何一条不过就抛**（拒绝带着坏表启动）。"""
    table = (registry.all() if isinstance(registry, ToolRegistry)
             else dict(registry or TOOL_REGISTRY))

    if set(table) != set(TOOL_NAMES):
        missing = sorted(set(TOOL_NAMES) - set(table))
        extra = sorted(set(table) - set(TOOL_NAMES))
        raise ValueError(
            f"工具登记表与内置的 9 个契约工具不一致：缺 {missing}，多 {extra}"
        )

    for name, definition in table.items():
        spec = definition.spec
        if spec.name != name:
            raise ValueError(f"登记键 {name!r} 与 spec.name={spec.name!r} 不一致")
        if spec.name not in TOOL_NAME_SET:
            raise ValueError(f"{name}: 不在 enums.TOOL_NAMES 里")
        if not spec.input_schema:
            raise ValueError(f"{name}: input_schema 为空（第 1 道保护会失效）")
        if not spec.result_summarizer or spec.result_summarizer not in S.SUMMARIZERS:
            raise ValueError(
                f"{name}: result_summarizer={spec.result_summarizer!r} 未登记在 "
                f"summarize.SUMMARIZERS（可选：{sorted(S.SUMMARIZERS)}）"
            )
        if definition.summarize is not S.SUMMARIZERS[spec.result_summarizer]:
            raise ValueError(f"{name}: 绑定的裁剪函数与声明的不一致")
        if definition.handler is None:
            raise ValueError(f"{name}: 没有实现（handlers.HANDLERS 里缺 {name}）")
        try:
            parse_rate_limit(spec.rate_limit)
        except ValueError as exc:
            raise ValueError(f"{name}: rate_limit 非法：{exc}") from exc
        if not spec.idempotent and spec.permission is ToolPermission.READ_ONLY:
            raise ValueError(f"{name}: 只读工具不该声明 idempotent=False（语义矛盾）")
        if spec.permission is not ToolPermission.READ_ONLY and spec.idempotent:
            raise ValueError(
                f"{name}: 写工具必须 idempotent=False —— 否则超时会触发『重试一次』，"
                f"而重试一次写操作可能造成第二次变更"
            )
        if not definition.capabilities:
            raise ValueError(
                f"{name}: 没声明能力 —— 授权会退化成「谁都调不了」或「谁都能调」"
            )

    # ★★ 能力模型必须**逐格复现** `AGENT_TOOL_MATRIX`（不得悄悄改变既有权限）
    verify_policy = ToolPolicy(allow_write=True)
    mismatches: list[str] = []
    for role in AGENT_TOOL_MATRIX:
        for name, definition in table.items():
            expected = name in AGENT_TOOL_MATRIX[role]
            actual = verify_policy.can_call(role, definition).allowed
            if expected != actual:
                mismatches.append(f"{role}/{name}: 矩阵={expected} 能力模型={actual}")
    if mismatches:
        raise ValueError(
            "能力模型与 AGENT_TOOL_MATRIX 不一致（改了能力表就必须同步矩阵，反之亦然）："
            + "；".join(mismatches[:6])
        )


def registry_summary(registry: Mapping[str, ToolDefinition] | ToolRegistry | None = None) -> list[dict[str, Any]]:
    """给日志/自检用的一行式摘要。"""
    table = (registry.all() if isinstance(registry, ToolRegistry)
             else dict(registry or TOOL_REGISTRY))
    return [definition.to_dict() for definition in table.values()]


validate_registry()
