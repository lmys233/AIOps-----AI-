"""`ToolExecutor` —— 工具调用的**唯一入口**。

```
call(agent_role, tool, params)
  │
  ├─ ① Schema 校验 ──────── 形状不对？ → TOOL_INVALID_PARAMS（模型可自修）
  ├─ ② 权限校验 ─────────── 能力交集 / 写能力开关 / 作用域掩码 → TOOL_FORBIDDEN
  ├─ ③ 语义校验 ─────────── 全量扫描/超窗口/超上限？ → TOOL_SCOPE_TOO_BROAD
  ├─ ③.5 单调守卫 ───────── 任一守卫拒绝即拒绝（不可被后续守卫翻回）
  ├─ ③.6 人工审批 ───────── L3+/声明需审批：没有审批通道就**拒绝执行**
  ├─ ④ 限流 ──────────────── 按（角色, 工具）令牌桶；桶空了 → TOOL_RATE_LIMITED（带 retry_after）
  └─ ⑤ 审计 + 执行 + 摘要化 ── 三种结局严格分开
```

--------------------------------------------------------------------------------
★ 顺序为什么不可变（这条比实现细节重要）
--------------------------------------------------------------------------------

| 交换 | 后果 |
|---|---|
| ①↔② | 先查权限，攻击者就能靠"参数错误 vs 无权限"的差异**探测有哪些工具存在** |
| ②↔③ | 无权限的调用会先被语义"解析"一遍 —— 等于给越权者一个免费的查询解析器 |
| ③↔④ | 超窗口的坏查询会先吃掉一个令牌：**坏请求把好请求的额度耗尽** |
| ④↔⑤ | 被限流/被拒的调用**不留审计** —— 而"谁在频繁越权"恰恰是最该留痕的信号 |

★ ③ 与 ④ 之间那两道**策略闸门**（单调守卫、人工审批）**不触碰数据源**，
  所以刻意放在限流**之前**：被策略拒掉的调用不该消耗额度。
  新增闸门不改变五道保护的**相对顺序**。

--------------------------------------------------------------------------------
★ 三种结局在这里的落地
--------------------------------------------------------------------------------

★ 只读工具与写工具**走同一入口但不同分支**（写工具要过审批），
  `degraded=True` 与 `data=[]` 的区别由 `ToolResult` 的契约校验器**强制**。
  三种结局（查了没异常 / 没查成 / 查询被拒）到 `ToolResult` 字段的映射
  **只在契约校验器那一处定义**（`common/contracts/tools.py`），这里不再复述一遍
  —— 复述必然与校验器漂移，而校验器才是权威。

--------------------------------------------------------------------------------
★ 高危管控
--------------------------------------------------------------------------------

| 机制 | 落点 |
|---|---|
| 写能力默认关闭 | ② 权限（`policy.can_call`）—— 内置写工具在册但不可调；扩展写工具注册即被拒 |
| 动作分级 | ③.6 审批闸门：`DEFAULT_ACTION_LEVEL[action_class]` 决定 L4 拒绝 / L3 需审批 / L2 放行 |
| 人工审批 | `approval_gate` 钩子；**没有钩子 → 拒绝执行**（没有"默认放行"这个选项） |
| 审批判据 | **只此一处**：`ToolPolicy._needs_approval`（本模块读 `decision.needs_approval`，不另算一份） |
| 单调守卫 | ③.5：任一守卫给理由即拒绝，后续守卫翻不回来 |
| 可解释 | 审计带 `tool_version` / `registry_revision` / `action_hash`（老审计能对上当时那一版） |
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Final, Mapping, Optional, Protocol, Sequence, runtime_checkable

from ..common.contracts import ToolResult
from ..observed.attempts import observed_call
from ..execution.runtime import record_denial
from ..common.action_hash import action_fingerprint
from ..common.enums import (
    DEFAULT_ACTION_LEVEL,
    ErrorCode,
    ToolPermission,
)
from .definition import ToolDefinition
from .errors import QueryRejected, SourceUnavailable, ToolRefused
from .handlers import ToolContext, invoke_handler
from .risk import higher_risk
from .policy import (
    CAP_WRITE,
    PolicyGuard,
    ToolPolicy,
    evaluate_guards,
    needs_approval,
)
from .rate_limit import RateLimiter
from .registry import BUILTIN_REGISTRY, ToolRegistry
from .schema_check import SchemaError, validate_params
from .semantics import check_semantics

__all__ = [
    "ToolExecutor",
    "AuditRecord",
    "AuditSink",
    "InMemoryAuditSink",
    "LoggingAuditSink",
    "SqlAuditSink",
    "ApprovalRequest",
    "ApprovalGate",
    "MAX_TIMEOUT_RETRIES",
]

logger = logging.getLogger("agent.tools.executor")

#: 超时**限重试 1 次**（且只对幂等工具）
MAX_TIMEOUT_RETRIES: Final[int] = 1


# ======================================================================
#  审计
# ======================================================================
@dataclass(frozen=True)
class AuditRecord:
    """一次工具调用的审计条目（字段与 `audit_log` 表对齐）。"""

    actor: str                      # `agent:<role>`
    action_class: str               # `tool_call`
    target: str                     # 工具名
    outcome: str                    # allowed / denied / failed / executed
    params: dict[str, Any]
    result_summary: str = ""
    error_code: str = ""
    elapsed_ms: float = 0.0
    risk_level: str = "L0"
    deny_reason: str = ""
    trace_id: str = ""
    incident_id: str = ""
    tool_version: int = 0                       # ★ 老审计能对上"当时是第几版工具"
    registry_revision: int = 0                  # ★ 当时注册表的状态
    action_hash: str = ""                       # ★ 写操作的参数指纹（防越权替换）
    occurred_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "occurred_at": self.occurred_at, "actor": self.actor,
            "action_class": self.action_class, "target": self.target,
            "outcome": self.outcome, "params": self.params,
            "result_summary": self.result_summary, "error_code": self.error_code,
            "elapsed_ms": round(self.elapsed_ms, 3), "risk_level": self.risk_level,
            "deny_reason": self.deny_reason, "trace_id": self.trace_id,
            "incident_id": self.incident_id, "tool_version": self.tool_version,
            "registry_revision": self.registry_revision,
            "action_hash": self.action_hash,
        }


@runtime_checkable
class AuditSink(Protocol):
    """审计出口。实现可以是 DB / 日志 / 内存（测试）。"""

    async def write(self, record: AuditRecord) -> None:  # pragma: no cover - 协议
        ...


class InMemoryAuditSink:
    """内存审计（离线单测用；也可当作"最近 N 条"的环形缓冲看）。"""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def write(self, record: AuditRecord) -> None:
        self.records.append(record)

    def outcomes(self) -> list[str]:
        return [r.outcome for r in self.records]


class LoggingAuditSink:
    """兜底审计出口：写结构化日志。

    ★ 没有数据库时**不能**"就不审计了" —— 那等于把可审计性绑在 DBA 身上。
      日志是弱一些的审计（可能被轮转掉），但比"什么都没有"强得多。
    """

    async def write(self, record: AuditRecord) -> None:
        logger.info("audit %s", json.dumps(record.to_dict(), ensure_ascii=False, default=str))


class SqlAuditSink:
    """把审计写进 `audit_log` 表。

    ★ 只用 INSERT：`aiops_runner` 角色在数据库层就没有 UPDATE/DELETE 权限
      （`deploy/postgres/init/02-runner-role.sql` 里还有断言守着）——
      所以"改审计"这件事不是靠应用层自律，而是**数据库不让你改**。
    """

    _SQL: Final[str] = """
    INSERT INTO audit_log
        (incident_id, actor, action_class, target, risk_level,
         params, result_summary, outcome, deny_reason, trace_id)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10)
    """

    def __init__(self, sql: Any) -> None:
        self._sql = sql

    async def write(self, record: AuditRecord) -> None:
        await self._sql.fetch(
            self._SQL,
            record.incident_id or None, record.actor, record.action_class,
            record.target, record.risk_level,
            json.dumps(record.params, ensure_ascii=False, default=str),
            record.result_summary[:500], record.outcome,
            record.deny_reason or None, record.trace_id or None,
        )


# ======================================================================
#  人工审批（危险动作的"询问"分支）
# ======================================================================
@dataclass(frozen=True)
class ApprovalRequest:
    """一次待审批的调用（审批人据此判断"批的是不是这一次"）。"""

    agent_role: str
    tool: str
    tool_version: int
    risk_level: str
    params: dict[str, Any]
    action_hash: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_role": self.agent_role, "tool": self.tool,
            "tool_version": self.tool_version, "risk_level": self.risk_level,
            "params": self.params, "action_hash": self.action_hash,
            "reason": self.reason,
        }


#: 审批钩子：返回 True = 批准。**没有钩子时高危调用一律拒绝**（不是放行）。
ApprovalGate = Callable[[ApprovalRequest], Awaitable[bool]]


def _action_hash(params: Mapping[str, Any]) -> str:
    """参数指纹 —— ★★ **与审批侧同一个实现**。

    传进来的是 `change_write` 的**入参**（含 `action_class` / `target` / `params`），
    所以它与 HTTP 层算出的 `action_hash` 是同一个值 —— 审批批的是哪一个动作，
    执行前核对的就是哪一个动作（防越权替换）。

    ★ 核对必须拿"**审批时冻结的那一份**"去比（`call()` 里的 `approved_params`）——
      拿同一个对象前后各算一次是指纹自己在比自己。
    """
    return action_fingerprint(params)


def _brief_params(params: Mapping[str, Any], *, max_value: int = 60,
                  max_items: int = 6) -> str:
    """把参数渲染成**短而完整**的一行（供 `raw_ref` 兜底用）。

    ★ 不做中间截断：`json.dumps(...)[:120]` 会在结构中间切断，
      留下一段既读不懂、又没法照着回查的文本。这里改成**逐值截断**——
      每个值要么完整给出、要么以 `…` 明确标出被截，键名永远完整。
    """
    parts: list[str] = []
    for key in sorted(params)[:max_items]:
        text = str(params[key]).replace("\r", " ").replace("\n", " ")
        if len(text) > max_value:
            text = text[:max_value] + "…"
        parts.append(f"{key}={text}")
    if len(params) > max_items:
        parts.append(f"…（共 {len(params)} 个参数）")
    return ", ".join(parts)


# ======================================================================
#  执行器
# ======================================================================
class ToolExecutor:
    """工具调用的唯一入口（Schema/权限/语义/守卫/审批/限流/审计/执行）。"""

    def __init__(
        self,
        *,
        context: Optional[ToolContext] = None,
        audit: Optional[AuditSink] = None,
        rate_limiter: Optional[RateLimiter] = None,
        registry: Optional[ToolRegistry | Mapping[str, ToolDefinition]] = None,
        policy: Optional[ToolPolicy] = None,
        guards: Sequence[PolicyGuard] = (),
        approval_gate: Optional[ApprovalGate] = None,
        clock: Any = time.monotonic,
        trace_id: str = "",
        incident_id: str = "",
    ) -> None:
        self.context = context or ToolContext()
        self.audit: AuditSink = audit or LoggingAuditSink()
        self.rate_limiter = rate_limiter or RateLimiter()
        self.registry: ToolRegistry = self._as_registry(registry, policy)
        self.guards: tuple[PolicyGuard, ...] = tuple(guards)
        self.approval_gate = approval_gate
        # ★ `clock or time.monotonic`：把 None 当"没注入"处理 ——
        #   否则一个手滑的 `clock=None` 会在**第一次调用时**炸成 TypeError（实测踩过）
        self._clock = clock or time.monotonic
        self.trace_id = trace_id
        self.incident_id = incident_id

    # ------------------------------------------------------------------
    @staticmethod
    def _as_registry(
        registry: Optional[ToolRegistry | Mapping[str, ToolDefinition]],
        policy: Optional[ToolPolicy],
    ) -> ToolRegistry:
        """接住三种输入：`ToolRegistry` / 普通映射 / `None`（用内置）。

        ★ 传映射时**包一层只读语义的注册表**：让"动态注册/能力授权/审计"
          这些能力对旧调用方也可用，而不必改它们的代码。
        """
        if isinstance(registry, ToolRegistry):
            return registry
        if registry is None:
            return BUILTIN_REGISTRY
        wrapped = ToolRegistry(policy=policy or ToolPolicy())
        for definition in registry.values():
            wrapped.register(definition, actor="system", reason="注入的映射式注册表")
        return wrapped

    # ------------------------------------------------------------------
    @property
    def policy(self) -> ToolPolicy:
        return self.registry.policy

    def with_context(self, context: ToolContext) -> "ToolExecutor":
        """派生一个**共享注册表/审计/限流、但用独立 `ToolContext`** 的执行器。

        ★★ 为什么必须能派生：`ToolContext` 里有**每次排查独有**的事实
          （`board` / `incident_id`），而执行器是**进程级**的（一个服务进程一个）。
          把 per-run 事实写进进程级对象 → 第二次请求会读到上一次的 incident：
          `evidence_lookup` 于是去**上一块黑板**找证据，找不到 → 报"证据读不回来"，
          而真相是"问错了事故"（表现为"所有结论都不可追溯"，极难定位）。

        ★ 浅拷贝是**故意的**：registry / audit / rate_limiter / policy / guards
          都是无状态或本就该全局共享的；只有 `context` 换成新的。
        """
        clone = copy.copy(self)
        clone.context = context
        return clone

    def llm_tools(self, agent_role: Optional[str] = None) -> list[dict[str, Any]]:
        """按角色导出可用的工具声明（**注册即可见**）。

        ★ 只导出该角色**有权调用**的工具：让模型看见它没有权限的工具，
          等于邀请它去撞 `TOOL_FORBIDDEN`（白烧一次调用，还会污染它的自我认知）。
          同时保证"证据独立性"——Log Agent 的候选里根本没有 promql。
        """
        if agent_role is None:
            definitions = list(self.registry.all().values())
        else:
            definitions = self.registry.visible_to(agent_role)
        return [{
            "type": "function",
            "function": {
                "name": definition.spec.name,
                "description": definition.spec.description,
                "parameters": definition.spec.input_schema,
            },
        } for definition in definitions]

    def spec_of(self, tool: str) -> Optional[ToolDefinition]:
        return self.registry.get(tool)

    async def call_query(self,agent_role,spec):
        from ..queries.binding import QueryParameters
        return await self.call(agent_role,spec.tool,QueryParameters(spec))

    # ------------------------------------------------------------------
    #  唯一入口
    # ------------------------------------------------------------------
    async def _observed_scope(self,role,tool,params,started):
        from ..observed.guard import protect
        try:return await protect(tool,params,self.spec_of(tool)),None
        except ToolRefused as exc:
            denied=await self._reject(agent_role=role,tool=tool,params=params,code=ErrorCode(exc.error_code),
                error_type=exc.error_type,summary=exc.message,outcome='denied',started=started,raw_ref='scope: denied')
            return params,denied
        except SourceUnavailable as exc:
            return params,ToolResult.unavailable(error_type=exc.error_type,summary=exc.message)
        except (PermissionError,ValueError) as exc:
            denied=await self._reject(agent_role=role,tool=tool,params=params,
                code=ErrorCode.TOOL_FORBIDDEN if isinstance(exc,PermissionError) else ErrorCode.TOOL_INVALID_PARAMS,
                error_type='ObservedScopeDenied',summary=str(exc),outcome='denied',started=started,raw_ref='scope: denied')
            return params,denied
        except Exception:
            return params,ToolResult.unavailable(error_type='RegistryUnavailable',summary='注册范围未能核实，停止本次查询')

    @observed_call
    @record_denial
    async def call(self, agent_role: str, tool: str, params: dict[str, Any]) -> ToolResult:
        """调用工具。**任何失败都返回 `ToolResult`，不向调用方抛异常。**

        ★ 为什么不抛异常：调用方是"取证 Agent 的循环"，它要做的是
          "把这一路标记为缺失并继续"，而不是崩掉整次排查。
          所以某一路不可用**不得**让整体失败。
        """
        started = self._clock()
        raw_params,denied=await self._observed_scope(agent_role,tool,params if params is not None else {},started)
        if denied is not None:return denied
        # ---- 前置：工具必须存在（这是"模型编了个工具名"的常见情况）----
        definition = self.registry.get(tool)
        if definition is None:
            return await self._reject(
                agent_role=agent_role, tool=tool, params=raw_params,
                code=ErrorCode.TOOL_INVALID_PARAMS, error_type="UnknownTool",
                summary=(f"没有名为 {tool!r} 的工具（可用：{self.registry.names()}）—— "
                         f"工具名必须是已注册的工具之一"),
                outcome="denied", started=started,
                raw_ref=f"unknown-tool: {tool}",
            )

        spec = definition.spec

        # ---- ① Schema 校验 ----
        try:
            schema_errors = validate_params(spec.input_schema, raw_params)
        except SchemaError as exc:
            logger.error("工具 %s 的 input_schema 自身有问题：%s", tool, exc)
            return await self._reject(
                agent_role=agent_role, tool=tool, params=raw_params,
                code=ErrorCode.INTERNAL_ERROR, error_type="BadToolSchema",
                summary=f"工具 {tool} 的入参 schema 有问题（实现缺陷，已记日志）",
                outcome="failed", started=started, raw_ref=f"schema: {tool}",
                definition=definition,
            )
        if schema_errors:
            return await self._reject(
                agent_role=agent_role, tool=tool, params=raw_params,
                code=ErrorCode.TOOL_INVALID_PARAMS, error_type="InvalidParams",
                summary="入参不符合 schema：" + "；".join(schema_errors[:4]),
                outcome="denied", started=started, definition=definition,
                raw_ref=self._raw_ref_hint(tool, raw_params),
            )

        # ---- ② 权限校验（能力交集 + 写能力开关 + 作用域掩码；fail-closed）----
        decision = self.policy.can_call(agent_role, definition)
        if not decision.allowed:
            return await self._reject(
                agent_role=agent_role, tool=tool, params=raw_params,
                code=ErrorCode.TOOL_FORBIDDEN, error_type="PermissionDenied",
                # ★ 只说"你没这个权限/为什么不能调"，**不说**怎么才能拿到
                summary=f"{agent_role or '<未知角色>'} 不能调用 {tool}：{decision.reason}",
                outcome="denied", started=started, definition=definition,
                raw_ref=self._raw_ref_hint(tool, raw_params),
            )

        # ---- ③ 语义校验（防全量扫描 / 时间窗 ≤24h / 返回上限）----
        semantic = check_semantics(tool, raw_params, spec.input_schema)
        if not semantic.ok:
            return await self._reject(
                agent_role=agent_role, tool=tool, params=raw_params,
                code=ErrorCode(semantic.error_code), error_type="SemanticRejected",
                summary=semantic.message + (f"（建议：{semantic.hint}）" if semantic.hint else ""),
                outcome="denied", started=started, definition=definition,
                raw_ref=self._raw_ref_hint(tool, raw_params),
            )
        normalized = semantic.params

        # ---- ③.5 单调守卫（任一守卫拒绝即拒绝，后续守卫翻不回来）----
        if self.guards:
            reasons = await evaluate_guards(self.guards, agent_role, definition, normalized)
            if reasons:
                return await self._reject(
                    agent_role=agent_role, tool=tool, params=raw_params,
                    code=ErrorCode.TOOL_FORBIDDEN, error_type="GuardDenied",
                    summary="被策略守卫拒绝：" + "；".join(reasons),
                    outcome="denied", started=started, definition=definition,
                    raw_ref=self._raw_ref_hint(tool, raw_params),
                )

        # ---- ③.6 高危动作：分级 + 人工审批（写能力工具）----
        is_write = spec.permission in (ToolPermission.WRITE,ToolPermission.DANGEROUS) or CAP_WRITE in definition.capabilities
        risk_level = self._effective_risk(definition, normalized) if is_write else definition.risk_level
        action_hash = _action_hash(raw_params) if is_write else ""
        #: ★ **审批看到的那一份参数**（冻结副本）。审批是个 `await`，期间调用方可能
        #:   就地改 `raw_params`；拿同一个对象前后比指纹等于自己比自己，
        #:   所以这里留一份深拷贝，审批回来再核对"现在要执行的还是不是这一份"。
        approved_params: dict[str, Any] = copy.deepcopy(dict(raw_params)) if is_write else {}
        # ★★ 审批判据**只此一处**：`policy.needs_approval(definition, risk_level)`。
        #   必须传**真实生效**的等级（含 action_class 升档）：读 `decision.needs_approval`
        #   （它只知道声明等级）会让"声明 L2 + action_class=scale 升到 L3"静默免审。
        needs = needs_approval(definition, risk_level)
        if is_write:
            blocked = await self._write_gate(
                agent_role=agent_role, definition=definition, params=approved_params,
                normalized=normalized, risk_level=risk_level,
                needs=needs, action_hash=action_hash, started=started,
            )
            if blocked is not None:
                return blocked
            # ★ 审批通过后核对：**冻结的那一份** vs 现在这一份（防"批的是一个参数、
            #   执行的是另一个"）。以前这里比的是 `_action_hash(params) != action_hash`，
            #   而两者来自同一个对象 → 恒等，"防越权替换"形同虚设。
            if dict(raw_params) != approved_params or _action_hash(raw_params) != action_hash:
                return await self._reject(
                    agent_role=agent_role, tool=tool, params=raw_params,
                    code=ErrorCode.APPROVAL_HASH_MISMATCH, error_type="ActionHashMismatch",
                    summary=("审批前后的参数指纹不一致 → 拒绝执行"
                             "（防越权替换）"),
                    outcome="denied", started=started, definition=definition,
                    raw_ref=self._raw_ref_hint(tool, raw_params),
                )

        # ---- ④ 限流 ----
        #
        # ★ `rate_limit` 的**注册期**已校验（policy 的 ①c）；这里再兜一层：
        #   限流器内部出任何意外都不能穿透给调用方 —— 本方法的契约是
        #   "任何失败都返回 ToolResult"。
        try:
            quota_tool='web_query:sdk' if tool=='web_query' and params.get('target_kind')=='sdk' else tool
            limit_decision = self.rate_limiter.check(agent_role, quota_tool, spec.rate_limit)
        except Exception as exc:  # noqa: BLE001
            logger.exception("限流器处理 %s 的 rate_limit=%r 时异常",
                             tool, spec.rate_limit)
            return await self._reject(
                agent_role=agent_role, tool=tool, params=raw_params,
                code=ErrorCode.INTERNAL_ERROR, error_type=f"RateLimitConfig:{type(exc).__name__}",
                summary=(f"限流配置无法处理（rate_limit={spec.rate_limit!r}）："
                         f"{type(exc).__name__} —— 这是**配置/代码问题**，不是数据源问题；"
                         f"这一路没查成"),
                outcome="failed", started=started, definition=definition,
                raw_ref=self._raw_ref_hint(tool, raw_params),
            )
        if not limit_decision.allowed:
            return await self._reject(
                agent_role=agent_role, tool=tool, params=raw_params,
                code=ErrorCode.TOOL_RATE_LIMITED, error_type="RateLimited",
                summary=(f"{limit_decision.reason}；"
                         f"约 {limit_decision.retry_after_sec:g}s 后可再试"),
                outcome="denied", started=started, definition=definition,
                raw_ref=self._raw_ref_hint(tool, raw_params),
            )

        # ---- ⑤ 审计 + 执行 + 摘要化 ----
        #
        # ★ 写工具：**先审计再执行**（审计写不进去就不执行）——
        #   "没有审计的写操作"是不可接受的。读工具无此要求：
        #   读不该因为审计库抖动而全线不可用（那会把可观测定成单点）。
        if is_write:
            ok = await self._write_audit(self._record(
                agent_role=agent_role, definition=definition, params=raw_params,
                outcome="allowed", summary="（写操作开始前登记）",
                risk_level=risk_level, action_hash=action_hash,
            ))
            if not ok:
                return await self._reject(
                    agent_role=agent_role, tool=tool, params=raw_params,
                    code=ErrorCode.TOOL_UNAVAILABLE, error_type="AuditUnavailable",
                    summary="审计通道不可用 → **拒绝执行写操作**（没有审计的写操作不可接受）",
                    outcome="denied", started=started, audit=False, definition=definition,
                    raw_ref=self._raw_ref_hint(tool, raw_params),
                )

        outcome = await self._execute(definition, normalized, started=started)

        await self._write_audit(self._record(
            agent_role=agent_role, definition=definition, params=raw_params,
            outcome=("executed" if (outcome.ok and is_write)
                     else "allowed" if outcome.ok else "failed"),
            summary=outcome.summary, error_code=outcome.error_code or "",
            elapsed_ms=(self._clock() - started) * 1000,
            risk_level=risk_level, action_hash=action_hash,
            deny_reason="" if outcome.ok else (outcome.error_type or ""),
        ))
        return outcome

    # ------------------------------------------------------------------
    #  高危动作的两道闸门
    # ------------------------------------------------------------------
    @staticmethod
    def _effective_risk(definition: ToolDefinition, params: Mapping[str, Any]) -> str:
        """写动作的**真实**风险等级：工具声明的等级是**下限**，`action_class` 决定实际等级。

        ★ 风险等级：L2 自动 + 事后审计，L3 强制人工审批，L4 直接拒绝。
          等级由**动作**决定（重启 vs 删命名空间），不由工具决定 ——
          所以这里查 `DEFAULT_ACTION_LEVEL`，查不到就按 **L3**（更谨慎的那一档）。

        ★★ 以前只在 `action_class` **缺省**时才回退到工具等级，
          **从来没有取两者的较大值** —— 于是 docstring 里"工具给的只是下限"是假话：
            工具声明 `risk_level="L2"`，调用方（或模型）在入参里写
            `action_class="read"`（schema 允许）→ `DEFAULT_ACTION_LEVEL["read"]` 是 L0
            → 返回值 **L0 < 工具自己的 L2 下限** → `_write_gate` 看到 L0/L1 就
            **不审批、不拒绝**，审计里也记成 L0。
          即"声明自己是 L2 的写工具，靠一个入参就能把自己降成 L0 通过闸门"——
          而分级恰恰是**只升不降**。现在取**较大值**：
          被管的对象（入参）不能把等级压到工具声明的下限之下。
        """
        action_class = str(params.get("action_class") or "").strip()
        if not action_class:
            return definition.risk_level
        derived = DEFAULT_ACTION_LEVEL.get(action_class, "L3")
        return higher_risk(definition.risk_level, derived)

    async def _write_gate(
        self, *, agent_role: str, definition: ToolDefinition, params: dict[str, Any],
        normalized: Mapping[str, Any], risk_level: str, needs: bool,
        action_hash: str, started: float,
    ) -> Optional[ToolResult]:
        """写动作的分级闸门：L4 拒绝 / L3 要审批 / 其余放行（还要过限流）。

        ★★ "要不要审批"**由调用方按唯一判据算好传进来**（`policy.needs_approval`）。
          本方法修前自己又写了一条 `definition.requires_approval or risk_level in ("L3",)`
          —— 同一件判据两份实现，改一处必漏另一处；而 `PolicyDecision.needs_approval`
          当时是**只写不读**（算出来就扔了，等于"看着在管、其实没人执行"）。
        """
        if risk_level == "L4":
            return await self._reject(
                agent_role=agent_role, tool=definition.name, params=params,
                code=ErrorCode.ACTION_FORBIDDEN_L4, error_type="ForbiddenL4",
                summary=(f"{params.get('action_class')!r} 属于 **L4 禁止类动作**："
                         f"Agent 一律不得执行，只记录审计"),
                outcome="denied", started=started, definition=definition,
                raw_ref=self._raw_ref_hint(definition.name, params),
            )

        if not needs:
            return None

        request = ApprovalRequest(
            agent_role=agent_role, tool=definition.name,
            tool_version=definition.version, risk_level=risk_level,
            # ★ 传**同一个对象**（不再 `dict(params)` 拷一份）：审批人如果改了它，
            #   "批的到底是什么"这件事就变了 —— `call()` 在审批返回后
            #   会拿冻结副本核对，改过就拒绝执行。
            #   ★ 给一份副本会让那道校验**永远看不出来**（那正是死代码的原因）。
            params=params,
            action_hash=action_hash,
            reason=str(params.get("reason") or ""),
        )
        if self.approval_gate is None:
            return await self._reject(
                agent_role=agent_role, tool=definition.name, params=params,
                code=ErrorCode.TOOL_FORBIDDEN, error_type="ApprovalRequired",
                summary=(f"{definition.name} 是 {risk_level} 高危动作，**必须人工审批**，"
                         f"而当前没有接入审批通道 → 拒绝执行"),
                outcome="denied", started=started, definition=definition,
                raw_ref=self._raw_ref_hint(definition.name, params),
            )

        try:
            approved = bool(await self.approval_gate(request))
        except Exception:  # noqa: BLE001 —— 审批通道自己炸了 → 拒绝（不能默认放行）
            logger.exception("审批通道异常：tool=%s action_hash=%s",
                             definition.name, action_hash)
            approved = False
        if not approved:
            return await self._reject(
                agent_role=agent_role, tool=definition.name, params=params,
                code=ErrorCode.TOOL_FORBIDDEN, error_type="ApprovalRejected",
                summary=(f"{definition.name} 的 {risk_level} 动作**未获人工批准**"
                         f"（action_hash={action_hash}）→ 不执行"),
                outcome="denied", started=started, definition=definition,
                raw_ref=self._raw_ref_hint(definition.name, params),
            )

        # ★ 参数指纹的**复核**在 `call()` 里做（那里同时看得见"审批冻结的那一份"与
        #   "现在这一份"）。本方法里 `params` 就是冻结副本，自比没有意义。
        return None

    # ------------------------------------------------------------------
    #  第 5 步：执行 + 摘要化（三种结局在这里分开）
    # ------------------------------------------------------------------
    async def _execute(self, definition, params, *, started):
        from .execution import run
        return await run(self,definition,params,started)

    def _record(
        self, *, agent_role: str, definition: ToolDefinition, params: Mapping[str, Any],
        outcome: str, summary: str = "", error_code: str = "", elapsed_ms: float = 0.0,
        risk_level: str = "", action_hash: str = "", deny_reason: str = "",
    ) -> AuditRecord:
        """造一条审计记录（带上工具版本与注册表版本 —— 老审计才能解释得通）。"""
        return AuditRecord(
            actor=f"agent:{agent_role}", action_class="tool_call", target=definition.name,
            outcome=outcome, params=dict(params), result_summary=summary,
            error_code=error_code, elapsed_ms=elapsed_ms,
            risk_level=risk_level or definition.risk_level,
            deny_reason=deny_reason, trace_id=self.trace_id, incident_id=self.incident_id,
            tool_version=definition.version,
            registry_revision=self.registry.revision,
            action_hash=action_hash,
        )

    async def _reject(
        self, *, agent_role: str, tool: str, params: dict[str, Any],
        code: ErrorCode, error_type: str, summary: str, outcome: str,
        started: float, raw_ref: str = "", audit: bool = True,
        definition: Optional[ToolDefinition] = None,
    ) -> ToolResult:
        """构造一个"被拒绝/未执行"的 `ToolResult`，并**照样留审计**。

        ★ 被拒绝的调用**必须留痕**：谁在反复越权、谁在被限流，
          是安全审计里最有价值的信号之一（"没人成功"不等于"没人试过"）。
        """
        result = ToolResult(
            ok=False, data=None, error_code=code.value, error_type=error_type,
            degraded=False, summary=summary, raw_ref=raw_ref,
            elapsed_ms=(self._clock() - started) * 1000,
        )
        if audit:
            version = definition.version if definition else 0
            await self._write_audit(AuditRecord(
                actor=f"agent:{agent_role}", action_class="tool_call", target=tool,
                outcome=outcome, params=params, result_summary=summary,
                error_code=code.value, deny_reason=error_type,
                elapsed_ms=result.elapsed_ms, trace_id=self.trace_id,
                incident_id=self.incident_id, tool_version=version,
                registry_revision=self.registry.revision,
            ))
        return result

    async def _write_audit(self, record: AuditRecord) -> bool:
        """写审计。返回是否成功（写工具据此决定要不要继续）。"""
        try:
            from ..observed.attempts import safe
            from dataclasses import replace
            record=replace(record,params=safe(record.params),result_summary=safe(record.result_summary))
            await self.audit.write(record)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("审计写入失败（outcome=%s target=%s）",
                             record.outcome, record.target)
            return False

    # ------------------------------------------------------------------
    def _raw_ref_hint(self, tool: str, params: Mapping[str, Any]) -> str:
        """给一个**可回查**的 `raw_ref`（即使没执行成功，也能照着重跑那次查询）。"""
        if tool in ("promql_read", "logql_read", "traceql_read") or "query" in params:
            return f"{tool}: {params.get('query', '')}"
        if tool == "change_read":
            return (f"sql: change_events | service={params.get('service', '*')}"
                    f" | change_id={params.get('change_id', '*')}")
        if tool == "topology_read":
            return f"topology: {params.get('service', '')}"
        if tool == "memory_read":
            return f"memory: {str(params.get('symptom', ''))[:60]}"
        if tool == "evidence_lookup":
            return f"board: evidence_id={params.get('evidence_id', '')}"
        return f"{tool}: {_brief_params(params)}"
