"""工具**策略**：能力化授权 + 高危管控 + 单调守卫。

★ 为什么把"角色 × 工具名"白名单升级成"能力"：
  硬编码白名单有两个问题 —— 新工具必须**改矩阵代码**才能授权（"动态注册"名存实亡，
  注册了也没人能用）；授权粒度是"名字"而不是"能干什么"（一个叫 `foo_read` 的新工具
  能拿到和 `promql_read` 一样的信任，只要名字像）。
  所以引入**能力（capability）**：工具声明"我需要什么能力"，角色声明"我有什么能力"，
  授权 = 交集。角色能力表是**安全口径**（写死在代码里、有断言守着），
  而工具的能力声明可以随注册进来 —— 但**不能给自己发能力之外的东西**。

★ 三条红线（都在导入期/注册期断言，不靠运行时自觉）：

1. `write` 能力**只给 `remediation_agent`**；
2. `raw_telemetry` 能力**不给 `rca_agent` / `critic_agent`**（只看黑板证据）；
3. `evidence_lookup` 能力**只给 `critic_agent`**（受限反查）。

★★ 这三条在**注册期**就校验：**不让危险组合进注册表**，比"进去之后再拦"稳得多。

★ 高危管控（默认关闭 + 审批 + 拒绝不可翻转）：

- 写能力默认关闭：`AIOPS_ALLOW_WRITE_TOOLS` 未开启时，扩展写工具**注册被拒**，
  内置写工具在册但**调用期拒绝**（保证契约工具清单完整）；
- 需要审批：`requires_approval=True`（或 `risk_level >= L3`）→ **必须有审批钩子**，
  没有就拒绝执行（没有"默认放行"这个选项）；
- 单调守卫：任一守卫给出理由即拒绝，**后续守卫无法把拒绝翻回允许**
  （对齐 dsh `ctx.tools.guard()` 的单调语义）；
- 作用域掩码：`deny(role, tool)` 只会让某角色的可见/可调集合**变小**
  （对齐 dsh `restrict()` 的交集语义）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Final, Mapping, Optional, Sequence

from .rate_limit import parse_rate_limit

if TYPE_CHECKING:  # pragma: no cover
    from .definition import ToolDefinition

__all__ = [
    "CAP_METRICS", "CAP_LOGS", "CAP_TRACES", "CAP_CHANGE", "CAP_TOPOLOGY",
    "CAP_MEMORY", "CAP_EVIDENCE_LOOKUP", "CAP_KNOWLEDGE", "CAP_RAW_TELEMETRY",
    "CAP_WRITE", "ALL_CAPABILITIES",
    "ROLE_CAPABILITIES", "WRITE_ROLE", "BLIND_TO_RAW_TELEMETRY",
    "PolicyDecision", "RegistrationDecision", "ToolPolicy", "needs_approval",
    "PolicyGuard",
]

# ======================================================================
#  能力集合
# ======================================================================
CAP_RAW_TELEMETRY: Final[str] = "raw_telemetry"   # 能否读原始遥测（区别于"读黑板证据"）
CAP_METRICS: Final[str] = "metrics"
CAP_LOGS: Final[str] = "logs"
CAP_TRACES: Final[str] = "traces"
CAP_CHANGE: Final[str] = "change"
CAP_TOPOLOGY: Final[str] = "topology"
CAP_MEMORY: Final[str] = "memory"
CAP_EVIDENCE_LOOKUP: Final[str] = "evidence_lookup"
CAP_KNOWLEDGE: Final[str] = "knowledge"
CAP_WRITE: Final[str] = "write"
CAP_DATABASE: Final[str] = 'database_read'
CAP_RUNTIME: Final[str] = 'runtime_read'
CAP_LIFECYCLE: Final[str] = 'lifecycle_read'
CAP_SDK_ACCESS: Final[str] = 'sdk_access_read'
CAP_ACTIVE_HTTP: Final[str] = 'active_http'
CAP_ALERTS: Final[str] = 'alerts_read'
CAP_PREFERENCE: Final[str] = 'preference_read'

# ★ 这里刻意**不设** "其它只读诊断"（diagnostic）这类兜底能力：
#   一个没有任何角色、也没有任何工具声明的能力名，只会制造"看起来有这条能力"的错觉。

ALL_CAPABILITIES: Final[frozenset[str]] = frozenset({
    CAP_RAW_TELEMETRY, CAP_METRICS, CAP_LOGS, CAP_TRACES, CAP_CHANGE,
    CAP_TOPOLOGY, CAP_MEMORY, CAP_EVIDENCE_LOOKUP, CAP_KNOWLEDGE,
    CAP_WRITE,CAP_DATABASE,CAP_RUNTIME,CAP_LIFECYCLE,CAP_SDK_ACCESS,CAP_ACTIVE_HTTP,CAP_ALERTS,CAP_PREFERENCE,
})

#: 角色 → 可访问的能力（★ 安全口径，改它等于改权限，必须有 review）
ROLE_CAPABILITIES: Final[Mapping[str, frozenset[str]]] = {
    'preference_agent':frozenset({CAP_PREFERENCE}),
    'command_agent':frozenset({CAP_ALERTS}),
    'probe_agent':frozenset({CAP_ACTIVE_HTTP}),
    'sdk_access_agent':frozenset({CAP_RAW_TELEMETRY,CAP_SDK_ACCESS}),
    'database_agent':frozenset({CAP_RAW_TELEMETRY,CAP_DATABASE}),
    'runtime_agent':frozenset({CAP_RAW_TELEMETRY,CAP_RUNTIME}),
    'lifecycle_agent':frozenset({CAP_RAW_TELEMETRY,CAP_LIFECYCLE}),
    # ★ 三个取证 Agent 持有 `raw_telemetry`（它们的工作就是读原始遥测），
    #   但各自**只多一项**自己那一路的能力 —— 所以 Metrics Agent 拿不到 logql。
    "metrics_agent": frozenset({CAP_RAW_TELEMETRY, CAP_METRICS}),
    "log_agent": frozenset({CAP_RAW_TELEMETRY, CAP_LOGS}),
    "trace_agent": frozenset({CAP_RAW_TELEMETRY, CAP_TRACES}),
    "change_agent": frozenset({CAP_CHANGE}),
    "topology_agent": frozenset({CAP_TOPOLOGY}),
    "memory_agent": frozenset({CAP_MEMORY}),
    "qa_agent": frozenset({CAP_KNOWLEDGE}),
    # ★ 后置角色：能看拓扑与记忆（结构化知识），但**拿不到原始遥测**
    "rca_agent": frozenset({CAP_TOPOLOGY, CAP_MEMORY}),
    "critic_agent": frozenset({CAP_EVIDENCE_LOOKUP}),
    "remediation_agent": frozenset({CAP_WRITE}),
    "report_agent": frozenset(),
}

#: 唯一持有写能力的角色
WRITE_ROLE: Final[str] = "remediation_agent"
#: 必须与原始遥测**隔离**的角色（只能看黑板证据）
BLIND_TO_RAW_TELEMETRY: Final[tuple[str, ...]] = ("rca_agent", "critic_agent")

# 导入期红线断言（改错了立刻炸，而不是等运行时越权）
assert CAP_WRITE in ROLE_CAPABILITIES[WRITE_ROLE], "写能力必须留给 remediation_agent"
for _role, _caps in ROLE_CAPABILITIES.items():
    if _role != WRITE_ROLE:
        assert CAP_WRITE not in _caps, f"{_role} 拿到了写能力"
    if _role in BLIND_TO_RAW_TELEMETRY:
        assert CAP_RAW_TELEMETRY not in _caps, (
            f"{_role} 拿到了 raw_telemetry（只能看黑板证据）"
        )
    assert _caps <= ALL_CAPABILITIES, f"{_role} 声明了未定义的能力：{_caps - ALL_CAPABILITIES}"


# ======================================================================
#  判定结果
# ======================================================================
@dataclass(frozen=True)
class PolicyDecision:
    """\"这个角色能不能调这个工具\"的判定 + **理由**（理由要能进审计）。

    ★ `needs_approval` 按工具**声明**的风险等级算（`can_call` 看不到本次的
      `action_class`，算不出"升档后的真实等级"）。写动作的闸门因此走
      `needs_approval(definition, 真实等级)` 那个函数 —— **判据只有那一份实现**，
      本字段是它在"声明等级"下的取值。
    """

    allowed: bool
    reason: str = ""
    needs_approval: bool = False
    risk_level: str = "L0"

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(frozen=True)
class RegistrationDecision:
    """注册期判定：允许注册吗？若允许，**实际**授予哪些角色。"""

    allowed: bool
    reason: str = ""
    effective_roles: tuple[str, ...] = ()
    revoked: tuple[str, ...] = ()          # 申请了但被能力交集/红线压掉的角色


class ToolPolicyError(ValueError):
    """策略本身配置错了（例如给不存在的角色授权）—— 属于实现缺陷，要立刻炸。"""


def needs_approval(definition: "ToolDefinition", risk_level: str) -> bool:
    """要不要人工审批 —— **判据只此一处**（`PolicyDecision.needs_approval` 与
    `executor._write_gate` 都走它）。

    Args:
        definition: 工具定义（`requires_approval` 是**显式**声明的那一档）。
        risk_level: **本次调用真实生效**的风险等级 —— 必须由调用方算好传进来
            （写动作的等级由 `action_class` 决定，见 `DEFAULT_ACTION_LEVEL`），
            **不许**在这里退回 `definition.risk_level`：那样会漏掉"工具声明 L2 +
            `action_class=scale` **升到** L3"这一类，表现为高危动作被静默放行。

    ★★ 为什么必须传 `risk_level` 而不是让本函数自己取：修前 executor 用的是
      `definition.requires_approval or effective_risk in ("L3",)`（**真实生效**的等级），
      而 `can_call` 那份用的是 `definition.risk_level`（**声明**的等级）。两者在
      "动作升档"时结论不同 —— 收敛时选错一边就等于放开审批闸门。
    """
    if definition.requires_approval:
        return True
    return risk_level in ("L3", "L4")


# ======================================================================
#  策略
# ======================================================================
@dataclass
class ToolPolicy:
    """能力化授权 + 高危管控。

    ★ `allow_write` 默认 **False**：写能力必须显式开启。
      这个默认值本身就是一条安全控制（"忘了配"的后果是"更安全"，而不是"更危险"）。
    """

    role_capabilities: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: dict(ROLE_CAPABILITIES))
    allow_write: bool = False
    #: 角色 → 额外拒绝的工具名（作用域掩码，**取交集**）
    denied: dict[str, set[str]] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def capabilities_of(self, role: str) -> frozenset[str]:
        """未知角色 → **空能力集**（fail-closed：查不到就是没有）。"""
        return frozenset(self.role_capabilities.get(role, frozenset()))

    def deny(self, role: str, tool: str) -> None:
        """给某角色加一条额外拒绝（只会让集合变小；对齐 dsh 的 `restrict()`）。"""
        self.denied.setdefault(role, set()).add(tool)

    def undeny(self, role: str, tool: str) -> None:
        self.denied.get(role, set()).discard(tool)

    # ------------------------------------------------------------------
    def can_call(self, role: str, definition: "ToolDefinition") -> PolicyDecision:
        """第 2 道保护的核心：**这个角色现在能不能调这个工具**。"""
        name = definition.name

        # ① 显式拒绝优先（作用域掩码；拒绝永远压过允许）
        if name in self.denied.get(role, set()):
            return PolicyDecision(False, f"{role} 被显式拒绝调用 {name}（作用域掩码）",
                                  risk_level=definition.risk_level)

        caps = self.capabilities_of(role)

        # ② 写能力：默认关闭（写能力闸门的第二道锁）
        if CAP_WRITE in definition.capabilities:
            if not self.allow_write:
                return PolicyDecision(
                    False,
                    f"{name} 需要写能力，而写能力当前**未启用**"
                    f"（需显式开启 AIOPS_ALLOW_WRITE_TOOLS）",
                    risk_level=definition.risk_level,
                )
            if role != WRITE_ROLE:
                return PolicyDecision(
                    False, f"{name} 是写工具，只允许 {WRITE_ROLE} 调用",
                    risk_level=definition.risk_level,
                )

        # ③ 能力交集（工具要求的能力必须被角色全覆盖）
        missing = set(definition.capabilities) - caps
        if missing:
            return PolicyDecision(
                False,
                f"{role} 无权调用 {name}：缺少能力 {sorted(missing)}"
                f"（它有权的能力：{sorted(caps) or '无'}）",
                risk_level=definition.risk_level,
            )

        return PolicyDecision(
            True, "", needs_approval=needs_approval(definition, definition.risk_level),
            risk_level=definition.risk_level,
        )

    # ------------------------------------------------------------------
    #  注册期校验（**高危控制在最前面**：别让危险工具进注册表）
    def validate_registration(self, definition: "ToolDefinition",
                              *, existing: Sequence[str] = ()) -> RegistrationDecision:
        """注册期校验（**高危控制在最前面**：别让危险工具进注册表）。

        Returns:
            `RegistrationDecision`；`allowed=False` 时注册方必须拒绝注册。
        """
        name = definition.name

        # ① 能力名必须是已知的（防止"自造一个能力"绕过交集判定）
        unknown = set(definition.capabilities) - ALL_CAPABILITIES
        from ..common.enums import ToolPermission
        if definition.spec.permission is ToolPermission.ACTIVE_HTTP and (
            CAP_ACTIVE_HTTP not in definition.capabilities or definition.spec.idempotent):
            return RegistrationDecision(False,'主动HTTP工具必须持有专门能力且禁止自动重放')
        if unknown:
            return RegistrationDecision(False, f"{name} 声明了未定义的能力 {sorted(unknown)}")

        # ①b ★★ **空能力 = 授权 fail-open，必须拒注册**
        #
        #  ③ 的能力交集判据是 `set(definition.capabilities) - caps`：
        #  `capabilities` 为空集时 `missing` **恒为空** → `can_call` 对**任何角色**
        #  返回放行（包括 report_agent / critic_agent 这种不该碰遥测的角色），
        #  而且 ⑤/⑥ 的 effective-role 计算会把它算成"人人都能调" —— **审计记录也跟着说错话**。
        #  `registry.py` 对内置工具有防"空能力"的保护，加载器这条路径没有 —— 这里补齐。
        if not definition.capabilities:
            return RegistrationDecision(
                False,
                f"{name} 没有声明任何能力（capabilities 为空）—— 授权会 fail-open："
                f"能力交集判据对空集恒成立，任何角色都能调它。"
                f"请在 @define_tool(capabilities=(...)) 里显式声明，例如 "
                f"capabilities=({CAP_METRICS!r},)",
            )

        # ①c ★ `rate_limit` 必须是**可解析**的
        #
        #  注册期不校验它的后果：`parse_rate_limit` 会在**每次调用**时抛
        #  `RateLimitConfigError`，而 `executor.call` 的限流那一步原本没有 try
        #  —— 一个拼写错误就让该工具**永久不可用**，还违背"任何失败都返回 ToolResult"
        #  的契约（调用方拿到的是异常而不是结果）。
        try:
            parse_rate_limit(definition.spec.rate_limit)
        except Exception as exc:  # noqa: BLE001 —— 这里就是要把它变成"拒绝注册"
            return RegistrationDecision(
                False,
                f"{name} 的 rate_limit={definition.spec.rate_limit!r} 无法解析"
                f"（{type(exc).__name__}: {exc}）—— 它会在每次调用时抛异常，"
                f"必须在注册期就拒绝（形如 60/min、10/s）",
            )

        # ② 申请的角色必须是真实角色（防止"发明一个超级角色"）
        roles = tuple(definition.granted_roles)
        bad_roles = [r for r in roles if r not in self.role_capabilities]
        if bad_roles:
            return RegistrationDecision(False, f"{name} 申请了未定义的角色 {bad_roles}")

        # ③ ★ 写能力：默认关闭 → **扩展工具直接拒注册**（内置契约工具例外，
        #    它必须在册，由调用期拦）
        if CAP_WRITE in definition.capabilities and definition.source != "builtin":
            if not self.allow_write:
                return RegistrationDecision(
                    False,
                    f"{name} 声明了写能力，而写能力未启用 —— 拒绝注册"
                    f"（fail-closed：危险工具不进注册表）",
                )

        # ④ 契约工具名保护（不得覆盖内置的 9 个契约工具）
        from .definition import CONTRACT_TOOL_NAMES

        if name in CONTRACT_TOOL_NAMES and definition.source != "builtin":
            return RegistrationDecision(
                False,
                f"{name} 是**契约工具名**，扩展工具不得占用"
                f"（要改契约工具的语义，请改内置定义并提升 version）",
            )

        if name in existing:
            return RegistrationDecision(False, f"{name} 已经注册过了（不得静默覆盖）")

        # ⑤ 红线：申请的角色里，哪些**实际上**拿不到（能力交集压掉）
        effective: list[str] = []
        revoked: list[str] = []
        for role in roles:
            caps = self.capabilities_of(role)
            missing = set(definition.capabilities) - caps
            if missing:
                revoked.append(f"{role}(缺 {sorted(missing)})")
            else:
                effective.append(role)

        # ⑥ 红线：高危工具不许"申请了角色但实际没人能调"地静默入表
        if CAP_WRITE in definition.capabilities:
            if [r for r in effective if r != WRITE_ROLE]:
                return RegistrationDecision(
                    False, f"{name} 是写工具，除 {WRITE_ROLE} 外不得授予任何角色")
            if WRITE_ROLE not in effective:
                revoked.append(f"{WRITE_ROLE}(写能力未启用或能力不足)")

        return RegistrationDecision(
            True, "", effective_roles=tuple(effective), revoked=tuple(revoked),
        )


# ======================================================================
#  单调守卫（对齐 dsh 的 ctx.tools.guard()）
# ======================================================================
GuardFn = Callable[[str, "ToolDefinition", Mapping[str, Any]], Awaitable[Optional[str]]]


@dataclass(frozen=True)
class PolicyGuard:
    """一条守卫：返回**理由字符串**即拒绝，返回 `None` 表示不表态。

    ★★ **单调**：只要有任何一条守卫给出理由，整次调用就被拒绝 ——
      后面的守卫**无法**把它翻回允许。这是与"可重排的 pre-execute 事件"的关键区别：
      可重排意味着"后注册的策略能覆盖先注册的策略"，
      而安全策略一旦能被覆盖，就等于没有。
    """

    name: str
    check: GuardFn


async def evaluate_guards(
    guards: Sequence[PolicyGuard], role: str, definition: "ToolDefinition",
    params: Mapping[str, Any],
) -> tuple[str, ...]:
    """跑完所有守卫，返回**全部**拒绝理由（空 = 放行）。

    ★ 故意跑完而不是短路：一次把问题说完，比让调用方"改一个报一个"省事得多；
      而且守卫之间不依赖执行顺序（单调性由此成立）。
    """
    reasons: list[str] = []
    for guard in guards:
        try:
            reason = await guard.check(role, definition, params)
        except Exception as exc:  # noqa: BLE001 —— 守卫自己炸了必须**拒绝**而不是放行
            reasons.append(f"守卫 {guard.name} 执行异常（{type(exc).__name__}）→ 按拒绝处理")
            continue
        if reason:
            reasons.append(f"{guard.name}: {reason}")
    return tuple(reasons)
