"""枚举与常量集中定义。

★ 本模块是**跨层枚举的唯一来源**。

为什么必须集中（真实事故）：
    同一组语义在不同层各写一套字面量，会出现"看起来一致、实际不匹配"的情况。
    当时文档用 ``human_verified``，而数据库 ``CHECK`` 约束用 ``verified_human``。
    后果不是"写库被拒"这么简单 —— 而是**权重字典的键永远匹配不上库里的值**，
    导致写入闸门**不报错但完全失效**（所有记忆静默写不进去）。
    因此：枚举在此唯一定义，业务代码禁止再写字面量。

取值以数据库 ``CHECK`` 约束为准：``deploy/postgres/init/01-schema.sql``。
"""

from __future__ import annotations

from enum import Enum
from typing import Final, FrozenSet

__all__ = [
    # ---- 枚举 ----
    "VerificationMethod",
    "Intent",
    "RiskLevel",
    "EvidenceSource",
    "EvidenceDirection",
    "HypothesisStatus",
    "ArbitDecision",
    "IntentSource",
    "ChangeStage",
    "ErrorCode",
    "TOOL_ERROR_CODES",
    "ChatStatus",
    "DeploymentForm",
    "ToolPermission",
    "ApprovalStatus",
    "AuditActionClass",
    "AuditOutcome",
    "ActorKind",
    "ActionClass",
    "ErrorDisposition",
    # ---- 常量 ----
    "DEFAULT_ACTION_LEVEL",
    "VERIFY_WEIGHTS",
    "VERIFY_GATE_THRESHOLD",
    "ERROR_RETRY_POLICY",
    "CHAT_STATUS_LABELS",
    "APPROVAL_TTL_MINUTES",
    "ALERT_SUPPRESS_WINDOW_MINUTES",
    "CHAT_MESSAGE_MIN",
    "CHAT_MESSAGE_MAX",
    "CHAT_MAX_ROUNDS_LIMIT",
    "CHAT_TOKEN_BUDGET_MAX",
    "ALTERNATIVE_ROUNDS_LIMIT",
    "BUSINESS_CLASSIFICATION_ROUNDS",
    "TARGETED_ROUNDS_LIMIT",
    "EVIDENCE_SOURCE_SET",
    "INTENT_SET",
    "RISK_LEVEL_SET",
    "ERROR_CODE_SET",
    "TOOL_NAMES",
    "TOOL_NAME_SET",
    "TOOL_UNLOCK_MAP",
    "READ_ONLY_TOOLS",
    "CHANGE_WRITE_INTENT",
    "AGENT_ROLES",
    "AGENT_TOOL_MATRIX",
    "DEGRADED_TOOLS_P1",
    # ---- 辅助 ----
    "as_str_set",
    "is_retryable",
]

# ==================================================================
#  基类：让枚举成员本身可与数据库字符串直接比较
# ==================================================================
class _StrEnum(str, Enum):
    """字符串枚举基类。

    ★ 继承 ``str`` 的目的：``VerificationMethod.VERIFIED_HUMAN == "verified_human"``
      为真，便于直接与数据库返回值、JSON 字段比较，无需到处 ``.value``。
      同时 ``str(x)`` 返回成员值而非 ``ClassName.MEMBER``
      （Python 3.12 的 ``StrEnum`` 行为），避免日志里出现意外形式。
    """

    def __str__(self) -> str:  # pragma: no cover - 简单转发
        return str(self.value)


def as_str_set(enum_cls: type[Enum]) -> FrozenSet[str]:
    """把枚举转为「成员值」的不可变集合，便于与外部字符串集合比对。"""
    return frozenset(member.value for member in enum_cls)


# ==================================================================
#  1. 验证方式（写入闸门 1 的输入）
#     ★ 取值必须与 01-schema.sql 的
#       CHECK (verification IN ('verified_human','verified_metrics','unverified','rejected'))
#       逐字一致
# ==================================================================
class VerificationMethod(_StrEnum):
    VERIFIED_HUMAN = "verified_human"
    VERIFIED_METRICS = "verified_metrics"
    UNVERIFIED = "unverified"
    REJECTED = "rejected"


# 写入闸门 1 的权重：验证方式 → 记忆可信度系数
VERIFY_WEIGHTS: Final[dict[str, float]] = {
    VerificationMethod.VERIFIED_HUMAN.value: 1.0,
    VerificationMethod.VERIFIED_METRICS.value: 0.85,
    VerificationMethod.UNVERIFIED.value: 0.40,
    VerificationMethod.REJECTED.value: 0.0,
}

# 权重低于此阈值 → 拒绝写入长期记忆
VERIFY_GATE_THRESHOLD: Final[float] = 0.7


# ==================================================================
#  2. 意图体系（6 类，MECE）
# ==================================================================
class Intent(_StrEnum):
    INCIDENT_RCA = "INCIDENT_RCA"
    METRIC_QUERY = "METRIC_QUERY"
    CHANGE_IMPACT = "CHANGE_IMPACT"
    REMEDIATION = "REMEDIATION"
    KNOWLEDGE_QA = "KNOWLEDGE_QA"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


# ==================================================================
#  3. 风险等级（L0~L4）
# ==================================================================
class RiskLevel(_StrEnum):
    L0 = "L0"   # 只读
    L1 = "L1"   # 干跑
    L2 = "L2"   # 低危可回滚，自动执行 + 事后审计
    L3 = "L3"   # 高危，强制人工审批
    L4 = "L4"   # 禁止，直接拒绝


# ==================================================================
#  4. 证据来源（Evidence.source，7 值，与各路数据源一一对应）
# ==================================================================
class EvidenceSource(_StrEnum):
    METRICS = "metrics"
    LOGS = "logs"
    TRACES = "traces"
    CHANGES = "changes"
    TOPOLOGY = "topology"
    MEMORY = "memory"
    HUMAN = "human"


# ==================================================================
#  5. 证据变化方向（Evidence.direction）
# ==================================================================
class EvidenceDirection(_StrEnum):
    UP = "up"
    DOWN = "down"
    FLAT = "flat"
    SPIKE = "spike"
    DROP = "drop"


# ==================================================================
#  6. 假设状态（Hypothesis.status）
# ==================================================================
class HypothesisStatus(_StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    EXCLUDED = "excluded"


# ==================================================================
#  7. 仲裁结论（ArbitResult.decision）
# ==================================================================
class ArbitDecision(_StrEnum):
    CONCLUDED = "concluded"
    NEED_TARGETED_EVIDENCE = "need_targeted_evidence"
    ABSTAIN = "abstain"


# ==================================================================
#  8. 意图识别来源（IntentResult.source，三阶段流水线）
# ==================================================================
class IntentSource(_StrEnum):
    RULE = "rule"
    VECTOR_MATCH = "vector_match"
    SMALL_MODEL = "small_model"
    LARGE_MODEL = "large_model"
    FALLBACK = "fallback"


# ==================================================================
#  8b. 对话落定状态（对外的 `status` 六分支）
# ==================================================================
class ChatStatus(_StrEnum):
    """一次问诊的落定状态（对外契约里的 `status`，六值）。

    ★ 请求级的状态字与"验证方式"里的 `rejected` **同字不同义**：
      这里是"这次排查的结局"，那里是"这条记忆的验证强度" —— 两处都必须走各自的枚举，
      否则改一个词会静默影响另一个（同一族缺陷的成因）。
    """

    CONCLUDED = "concluded"
    #: ★★ 契约修订 21：已按用户所问给出结果（指标数值 / 现状快照）。
    #: 与 `concluded` 的边界是语义：后者唯一表示"已定位根因"（`root_cause` 非空）；
    #: 把"QPS 是多少"这类**值问答**记成 `abstained`（未得出结论）是假话 —— 数已经算出来了。
    ANSWERED = "answered"
    CLARIFY = "clarify"
    ABSTAINED = "abstained"
    BUDGET_EXHAUSTED = "budget_exhausted"
    REJECTED = "rejected"
    FAILED = "failed"


#: 状态 → 给用户看的中文标签（**唯一来源**：答案白话化与导出件都读它）
CHAT_STATUS_LABELS: Final[dict[str, str]] = {
    ChatStatus.CONCLUDED.value: "已定位根因",
    ChatStatus.ANSWERED.value: "已给出结果",   # 值问答：答了（≠ 已定位根因）
    ChatStatus.CLARIFY.value: "需要补充信息",
    ChatStatus.ABSTAINED.value: "未得出结论",
    ChatStatus.BUDGET_EXHAUSTED.value: "预算用尽",
    ChatStatus.REJECTED.value: "已拒绝",
    ChatStatus.FAILED.value: "系统错误",
}


# ==================================================================
#  9. 变更执行阶段（ChangeResult.stage，对应变更执行 5 步）
# ==================================================================
class ChangeStage(_StrEnum):
    DRY_RUN = "dry_run"
    DIFF = "diff"
    SYNC = "sync"
    VERIFIED = "verified"
    ROLLBACK = "rollback"


# ==================================================================
#  10b. 部署形态（**事实**：被观测系统跑在什么形态里）
# ==================================================================
class DeploymentForm(_StrEnum):
    """被观测系统的部署形态 —— 用户可见文案里的**机制名词由它决定**。

    ★★ 为什么要把它做成一等公民：方案/答案里会出现"Pod / 副本 / 命名空间"这类机制名词，
      它们**必须**来自事实。配了 k8s 才谈 Pod；没配（`UNKNOWN`）就用中性措辞，
      并**明说**"部署形态未配置" —— 靠"禁词表"是治标，靠事实才治本。
    """

    HOST_PROCESS = "host_process"     #: 宿主上直接跑进程（本项目当前形态）
    K8S = "k8s"                       #: 容器编排
    UNKNOWN = "unknown"               #: 没配置 → 只许用中性措辞


# ==================================================================
#  11. 工具权限（ToolSpec.permission）
# ==================================================================
class ToolPermission(_StrEnum):
    READ_ONLY = "read_only"
    ACTIVE_HTTP = "active_http"
    WRITE = "write"
    DANGEROUS = "dangerous"


# ==================================================================
#  11. 审批状态（与 approval_requests 的 CHECK 约束对齐）
# ==================================================================
class ApprovalStatus(_StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"


# ==================================================================
#  12. 审计动作类别（与 audit_log 的 CHECK 约束对齐）
# ==================================================================
class AuditActionClass(_StrEnum):
    TOOL_CALL = "tool_call"
    CHANGE_PROPOSAL = "change_proposal"
    APPROVAL = "approval"
    EXECUTE = "execute"
    ROLLBACK = "rollback"
    FAULT_INJECTION = "fault_injection"
    BUSINESS_OPERATION = "business_operation"
    MEMORY_WRITE = "memory_write"
    REPORT = "report"


class AuditOutcome(_StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


class ActorKind(_StrEnum):
    """审计中的 actor 前缀，decision 的 ``decided_by`` 等字段以它开头。

    ★ 为什么单独定义：红线要求**审批人不能是 Agent 自身**，
      实现上就是比对 actor 前缀（禁止 ``agent:`` 开头的 actor 做审批）。
      前缀散落各处极易写错。
    """

    AGENT = "agent:"       # agent:<role>
    HUMAN = "human:"       # human:<name>
    SYSTEM = "system"
    SCENARIO = "scenario:"  # 故障注入脚本


class ActionClass(_StrEnum):
    """变更动作类型 —— 风险分级的输入。

    ★ 归属说明：本类虽是"动作类型"，但本质是**枚举**，
      按"跨层枚举集中在单一模块"的约定，故定义在 enums.py，
      而非 contracts.py。契约对象 `ProposedAction` 引用它。

    ★ 为什么必须显式枚举化：
      早期设计只处理 FORBIDDEN/READ_ONLY/DRY_RUNNABLE 三类分类，
      导致 L2 类动作（restart_pod/rollback_config/drain_instance）走到一半
      `level` 变量未赋值 → UnboundLocalError。
      枚举化 + 穷尽映射可从根本上杜绝"漏一个就走空分支"。

    分级：L0 只读 / L1 干跑 / L2 低危可回滚 / L3 高危 / L4 禁止
    """

    # ---- L0 只读 ----
    READ = "read"
    # ---- L1 干跑 ----
    DRY_RUN = "dry_run"
    # ---- L2 低危可回滚 ----
    RESTART_POD = "restart_pod"
    ROLLBACK_CONFIG = "rollback_config"
    DRAIN_INSTANCE = "drain_instance"
    # ---- L3 高危（强制人工审批）----
    SCALE = "scale"
    PATCH_ROUTE = "patch_route"
    PATCH_RESOURCE_LIMIT = "patch_resource_limit"
    CROSS_NAMESPACE = "cross_namespace"
    DATA_OPERATION = "data_operation"
    # ---- L4 禁止（直接拒绝）----
    DELETE_NAMESPACE = "delete_namespace"
    MODIFY_RBAC = "modify_rbac"
    MODIFY_NETWORK_POLICY = "modify_network_policy"
    CONTROL_PLANE = "control_plane"
    DELETE_PVC = "delete_pvc"


# ★ DEFAULT_ACTION_LEVEL：action_class → **起点**风险等级
#
#  正确做法是**每个 action_class 都有明确的起点等级**，
#  classify_risk() 从该起点向上调整，而不是自己判断落进哪个分支。
#
#  因此本映射必须穷尽 ActionClass 的全部成员；
#  文件末尾有穷尽性断言，直接防住上面记录的那次 UnboundLocalError 回归。
DEFAULT_ACTION_LEVEL: Final[dict[str, str]] = {
    # L0 只读
    ActionClass.READ.value: "L0",
    # L1 干跑
    ActionClass.DRY_RUN.value: "L1",
    # L2 低危可回滚
    ActionClass.RESTART_POD.value: "L2",
    ActionClass.ROLLBACK_CONFIG.value: "L2",
    ActionClass.DRAIN_INSTANCE.value: "L2",
    # L3 高危
    ActionClass.SCALE.value: "L3",
    ActionClass.PATCH_ROUTE.value: "L3",
    ActionClass.PATCH_RESOURCE_LIMIT.value: "L3",
    ActionClass.CROSS_NAMESPACE.value: "L3",
    ActionClass.DATA_OPERATION.value: "L3",
    # L4 禁止
    ActionClass.DELETE_NAMESPACE.value: "L4",
    ActionClass.MODIFY_RBAC.value: "L4",
    ActionClass.MODIFY_NETWORK_POLICY.value: "L4",
    ActionClass.CONTROL_PLANE.value: "L4",
    ActionClass.DELETE_PVC.value: "L4",
}

# 穷尽性断言：ActionClass 的每个成员都必须在映射表里有起点等级。
# ★ 这条断言直接对应上面记录的 UnboundLocalError 回归防护。
if set(DEFAULT_ACTION_LEVEL) != {m.value for m in ActionClass}:  # pragma: no cover
    _missing = sorted({m.value for m in ActionClass} - set(DEFAULT_ACTION_LEVEL))
    raise AssertionError(
        f"DEFAULT_ACTION_LEVEL 必须穷尽 ActionClass 的全部成员，缺失: {_missing}"
    )
if set(DEFAULT_ACTION_LEVEL.values()) - as_str_set(RiskLevel):  # pragma: no cover
    raise AssertionError("DEFAULT_ACTION_LEVEL 中存在非法的风险等级取值")


# ==================================================================
#  13. 错误码（与数据库 CHECK 约束对齐的 18 个错误码）
# ==================================================================
class ErrorCode(_StrEnum):
    """错误码全集：**工具/模型级**（前 13 个）+ **HTTP 请求级**（后 5 个）。

    ★ 两族的区别是**谁产生它**：工具/模型级的码写进 `ToolResult.error_code`，
      按可重试矩阵处置；请求级的码是"这次请求本身不成立"（参数/鉴权/找不到/重复），
      由 HTTP 层直接返回 —— 它们**不进**工具结果的 `error_code`。
    """

    TOOL_INVALID_PARAMS = "TOOL_INVALID_PARAMS"
    TOOL_FORBIDDEN = "TOOL_FORBIDDEN"
    TOOL_SCOPE_TOO_BROAD = "TOOL_SCOPE_TOO_BROAD"
    TOOL_RATE_LIMITED = "TOOL_RATE_LIMITED"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_SCHEMA_INVALID = "LLM_SCHEMA_INVALID"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_HASH_MISMATCH = "APPROVAL_HASH_MISMATCH"
    ACTION_FORBIDDEN_L4 = "ACTION_FORBIDDEN_L4"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # ---- HTTP 请求级（对外接口的拒绝码；能力页与契约表都列出它们）----
    INVALID_PARAMS = "INVALID_PARAMS"
    UNAUTHORIZED = "UNAUTHORIZED"
    NOT_FOUND = "NOT_FOUND"
    INCIDENT_NOT_FOUND = "INCIDENT_NOT_FOUND"
    CONFLICT = "CONFLICT"


#: **工具/模型级**错误码（`ToolResult.error_code` 只认这一族）——
#: 请求级的码由 HTTP 层返回，混进工具结果会让"这一路信号缺失"与"这次请求没成立"分不开。
TOOL_ERROR_CODES: Final[FrozenSet[str]] = frozenset({
    ErrorCode.TOOL_INVALID_PARAMS.value,
    ErrorCode.TOOL_FORBIDDEN.value,
    ErrorCode.TOOL_SCOPE_TOO_BROAD.value,
    ErrorCode.TOOL_RATE_LIMITED.value,
    ErrorCode.TOOL_TIMEOUT.value,
    ErrorCode.TOOL_UNAVAILABLE.value,
    ErrorCode.LLM_TIMEOUT.value,
    ErrorCode.LLM_SCHEMA_INVALID.value,
    ErrorCode.BUDGET_EXHAUSTED.value,
    ErrorCode.APPROVAL_EXPIRED.value,
    ErrorCode.APPROVAL_HASH_MISMATCH.value,
    ErrorCode.ACTION_FORBIDDEN_L4.value,
    ErrorCode.INTERNAL_ERROR.value,
})


class ErrorDisposition(_StrEnum):
    """错误的整体处置倾向（三条强制规则的可执行化）。"""

    RETRYABLE = "retryable"      # 可重试（有明确重试上限）
    DEGRADABLE = "degradable"    # 不可重试，但必须降级继续（不得整体失败）
    TERMINAL = "terminal"        # 不可重试，终止当前动作（含安全告警）


# ★ 「错误分类与处置矩阵」的可执行形式
#   结构: 错误码 → {retryable, disposition, limit, behavior, user_visible}
#   ★ retryable 一列最关键 —— 客户端与 Agent 都据此决策退避策略。
#     写错会导致：该重试的不重试（漏排查）或不该重试的狂重试（雪崩）。
ERROR_RETRY_POLICY: Final[dict[str, dict[str, object]]] = {
    # ---- 参数 / 范围 / 限流：可重试，用户不可见（内部自愈）----
    ErrorCode.TOOL_INVALID_PARAMS.value: {
        "retryable": True,
        "disposition": ErrorDisposition.RETRYABLE.value,
        "limit": 1,
        "behavior": "带错误信息让模型自修参数",
        "user_visible": False,
    },
    ErrorCode.TOOL_SCOPE_TOO_BROAD.value: {
        "retryable": True,
        "disposition": ErrorDisposition.RETRYABLE.value,
        "limit": 1,
        "behavior": "自动缩小时间窗或补充 label 过滤后重试",
        "user_visible": False,
    },
    ErrorCode.TOOL_RATE_LIMITED.value: {
        "retryable": True,
        "disposition": ErrorDisposition.RETRYABLE.value,
        "limit": 3,
        "behavior": "指数退避，最多重试 3 次",
        "user_visible": False,
    },
    # ---- 权限：不可重试（重试无意义，且不得提示绕过）----
    ErrorCode.TOOL_FORBIDDEN.value: {
        "retryable": False,
        "disposition": ErrorDisposition.TERMINAL.value,
        "limit": 0,
        "behavior": "明确告知无权限，且不提示如何绕过",
        "user_visible": False,
    },
    # ---- 超时 / 结构非法：限 1 次重试，用户可见降级提示 ----
    ErrorCode.TOOL_TIMEOUT.value: {
        "retryable": True,
        "disposition": ErrorDisposition.RETRYABLE.value,
        "limit": 1,
        "behavior": "重试 1 次，仍失败则标记该路信号缺失并提示缩小查询范围",
        "user_visible": True,
    },
    ErrorCode.LLM_TIMEOUT.value: {
        "retryable": True,
        "disposition": ErrorDisposition.RETRYABLE.value,
        "limit": 1,
        "behavior": "重试 1 次，仍失败则标记该路信号缺失",
        "user_visible": True,
    },
    ErrorCode.LLM_SCHEMA_INVALID.value: {
        "retryable": True,
        "disposition": ErrorDisposition.RETRYABLE.value,
        "limit": 1,
        "behavior": "重试 1 次，仍失败则启用兜底策略",
        "user_visible": True,
    },
    # ---- 数据源不可用：不可重试，必须降级继续 ----
    ErrorCode.TOOL_UNAVAILABLE.value: {
        "retryable": False,
        "disposition": ErrorDisposition.DEGRADABLE.value,
        "limit": 0,
        "behavior": "降级：标记该路信号缺失，继续用其余信号给出结论或拒答",
        "user_visible": True,
    },
    # ---- 预算 / 审批：不可重试 ----
    ErrorCode.BUDGET_EXHAUSTED.value: {
        "retryable": False,
        "disposition": ErrorDisposition.DEGRADABLE.value,
        "limit": 0,
        "behavior": "降级输出当前最佳候选 + 不确定性说明",
        "user_visible": True,
    },
    ErrorCode.APPROVAL_EXPIRED.value: {
        "retryable": False,
        "disposition": ErrorDisposition.TERMINAL.value,
        "limit": 0,
        "behavior": "转为仅输出结论不执行",
        "user_visible": True,
    },
    # ---- 安全类：不可重试，必须告警 ----
    ErrorCode.APPROVAL_HASH_MISMATCH.value: {
        "retryable": False,
        "disposition": ErrorDisposition.TERMINAL.value,
        "limit": 0,
        "behavior": "拒绝执行并触发安全告警（防越权替换动作）",
        "user_visible": True,
    },
    ErrorCode.ACTION_FORBIDDEN_L4.value: {
        "retryable": False,
        "disposition": ErrorDisposition.TERMINAL.value,
        "limit": 0,
        "behavior": "直接拒绝，记录审计",
        "user_visible": True,
    },
    # ---- 内部异常 ----
    ErrorCode.INTERNAL_ERROR.value: {
        "retryable": False,
        "disposition": ErrorDisposition.TERMINAL.value,
        "limit": 0,
        "behavior": "记录完整堆栈到审计，不向前端泄露内部细节，仅给通用提示 + trace_id",
        "user_visible": True,
    },
    # ---- HTTP 请求级：这次请求本身不成立（由 HTTP 层直接返回，不进工具结果）----
    ErrorCode.INVALID_PARAMS.value: {
        "retryable": False,
        "disposition": ErrorDisposition.TERMINAL.value,
        "limit": 0,
        "behavior": "按字段逐条说明哪里不合法（不泄露内部细节），由调用方改正后重发",
        "user_visible": True,
    },
    ErrorCode.UNAUTHORIZED.value: {
        "retryable": False,
        "disposition": ErrorDisposition.TERMINAL.value,
        "limit": 0,
        "behavior": "未通过鉴权：拒绝且不说明如何绕过",
        "user_visible": True,
    },
    ErrorCode.NOT_FOUND.value: {
        "retryable": False,
        "disposition": ErrorDisposition.TERMINAL.value,
        "limit": 0,
        "behavior": "目标（本地会话等）不存在：如实说明，不编一条空记录",
        "user_visible": True,
    },
    ErrorCode.INCIDENT_NOT_FOUND.value: {
        "retryable": False,
        "disposition": ErrorDisposition.TERMINAL.value,
        "limit": 0,
        "behavior": "事故不在留存期内：如实说明「查不到」而不是「没有异常」",
        "user_visible": True,
    },
    ErrorCode.CONFLICT.value: {
        "retryable": False,
        "disposition": ErrorDisposition.TERMINAL.value,
        "limit": 0,
        "behavior": "重复请求（同一轮次 id，或抖动窗口内同内容）：直接拒绝并说明换新 id 或稍后再问",
        "user_visible": True,
    },
}


def is_retryable(code: "ErrorCode | str") -> bool:
    """该错误码是否可重试（供退避逻辑统一调用）。"""
    key = code.value if isinstance(code, ErrorCode) else str(code)
    entry = ERROR_RETRY_POLICY.get(key)
    return bool(entry and entry.get("retryable"))


# ==================================================================
#  14. 数量与窗口常量
# ==================================================================
APPROVAL_TTL_MINUTES: Final[int] = 15          # 审批 15 分钟后过期
ALERT_SUPPRESS_WINDOW_MINUTES: Final[int] = 5  # 告警抑制窗口

CHAT_MESSAGE_MIN: Final[int] = 1
CHAT_MESSAGE_MAX: Final[int] = 4000
CHAT_MAX_ROUNDS_LIMIT: Final[int] = 5          # 上限强制 5
CHAT_TOKEN_BUDGET_MAX: Final[int] = 500_000

# 编排终止：连续两轮无新增有效证据则收敛；
# 定向二次取证最多 2 轮（第 1 轮广度 + 最多 2 轮定向）
ALTERNATIVE_ROUNDS_LIMIT: Final[int] = 2
BUSINESS_CLASSIFICATION_ROUNDS: Final[int] = 3
TARGETED_ROUNDS_LIMIT: Final[int] = 2


# ==================================================================
#  15. 工具名（共 10 个）
#     ★ 工具名**不得绑定存储实现**：用 topology_read 而非 cypher_read
#       （换存储实现时 Agent 侧应零改动）
# ==================================================================
TOOL_NAMES: Final[tuple[str, ...]] = (
    "promql_read",       # Prometheus，Metrics Agent
    "logql_read",        # Loki，Log Agent
    "traceql_read",      # Tempo，Trace Agent
    "trace_read",        # ★ 按 trace_id 精确取**整条** trace（下钻到断点 span）
    "change_read",       # change_events 表，Change Agent
    "topology_read",     # 拓扑，Topology Agent（★ 不是 cypher_read）
    "memory_read",       # 记忆，Memory Agent
    "evidence_lookup",   # ★ 仅 Critic 可用，且只能反查已有 evidence_id
    "kb_search",         # 有界知识检索，与现场遥测独立
    "kb_document_read",
    "change_write",      # ★ 唯一写工具，仅 REMEDIATION 意图解锁
    "raw_read",          # ★ 只读本地磁盘上的原始数据副本（不重发上游请求）
    "series_stats", "series_rate", "series_change", "series_slope", "series_compare",
    "math_eval",            # ★ 契约修订 21：数据运算工具（纯本地；值必须由它们算出来）
)

TOOL_NAME_SET: Final[FrozenSet[str]] = frozenset(TOOL_NAMES)

# ==================================================================
# 16. 工具解锁表（意图 → 解锁工具）
#
#  ★ 为什么这是一张**表**而不是散在各处的 if：
#    "REMEDIATION 是唯一解锁写能力的意图"是本项目的红线之一。
#    红线必须能被**机械检查** —— 表能被单测断言，散落的 if 只能靠人读代码。
#
#  ★ RCA 的全量遥测工具不包括知识工具和 Critic 反查：
#    · `evidence_lookup`：限定"仅 Critic 可用，且只能反查已有 evidence_id"
#    · 知识工具仅由 KNOWLEDGE_QA / qa_agent 获取，不能充当现场证据。
# ==================================================================
READ_ONLY_TOOLS: Final[tuple[str, ...]] = (
    "promql_read",
    "logql_read",
    "traceql_read",
    "change_read",
    "topology_read",
    "memory_read",
    "raw_read",          # ★ 它也是"读"：读的是本地副本，不碰被观测系统
)

CHANGE_WRITE_INTENT: Final[Intent] = Intent.REMEDIATION

TOOL_UNLOCK_MAP: Final[dict[str, tuple[str, ...]]] = {
    Intent.INCIDENT_RCA.value: READ_ONLY_TOOLS,
    Intent.METRIC_QUERY.value: ("promql_read", "raw_read", "series_stats", "series_rate",
                                "series_change", "series_slope", "series_compare", "math_eval"),
    Intent.CHANGE_IMPACT.value: ("change_read", "topology_read"),
    Intent.REMEDIATION.value: READ_ONLY_TOOLS + ("change_write",),
    Intent.KNOWLEDGE_QA.value: ("kb_search", "kb_document_read"),
    Intent.OUT_OF_SCOPE.value: (),
}

assert set(TOOL_UNLOCK_MAP) == {m.value for m in Intent}, (
    "TOOL_UNLOCK 未覆盖全部6个意图"
)
for _intent, _tools in TOOL_UNLOCK_MAP.items():
    _unknown = [t for t in _tools if t not in TOOL_NAME_SET]
    assert not _unknown, f"{_intent} 解锁了未定义的工具 {_unknown}"

assert  TOOL_UNLOCK_MAP[Intent.OUT_OF_SCOPE.value] == (), (
    "OUT_OF_SCOPE 解锁**空集** -- 越界输入拿不到任何工具"
)

# 写工具只能由REMEDIATION 解锁
for _intent, _tools in TOOL_UNLOCK_MAP.items():
    if _intent != CHANGE_WRITE_INTENT.value:
        assert  "change_write" not in _tools, f"{_intent} 不该解锁 change_write"


#: 来源取证、知识、仲裁与报告角色；按角色白名单是第二道保护。
AGENT_ROLES: Final[tuple[str, ...]] = (
    'probe_agent',
    'sdk_access_agent',
    'database_agent','runtime_agent','lifecycle_agent',
    "metrics_agent", "log_agent", "trace_agent",
    "change_agent", "topology_agent",
    "memory_agent", "rca_agent", "critic_agent",
    "qa_agent",
    "remediation_agent",
    "report_agent",
)

#: 角色 → 允许调用的工具（**白名单**，不在表里 = 一律拒绝）。
#:
#: ★★ 三条设计约束（每条后面都有对应的运行时断言）：
#:
#:   1. **每个取证 Agent 只能看自己那一路**（Metrics 看不到日志、Log 看不到指标）。
#:      这不是"最小权限"的装饰 —— 它是**证据独立性**的实现手段：
#:      若 Metrics Agent 能查日志，它就会把"日志里也提到了数据库"当自己的发现，
#:      于是"两路独立证据互相印证"变成"一个人说了两遍"。
#:   2. **RCA / Critic 不得持有任何原始遥测工具**：它们只看黑板上**已经结构化**的证据。
#:      证据进黑板时已经带了 entity/signal/direction/onset，直接读原始数据会绕过
#:      "证据必须先落黑板"这条主线（那正是"结论可追溯"的前提）。
#:   3. **写工具只给 remediation_agent**；`evidence_lookup` 只给 critic_agent
#:      （受限反查，且**只能反查已存在的 evidence_id**，不得新增假设）。
AGENT_TOOL_MATRIX: Final[dict[str, tuple[str, ...]]] = {
    'probe_agent':(),  # Active HTTP tools are extensions with their own capability.
    'sdk_access_agent':('sdk_access_read',),
    'database_agent':('db_object_read','db_context_read','migration_read','ddl_audit_read',
        'db_activity_read','db_lock_read','db_deadlock_read','db_slow_query_read','db_plan_read'),
    'runtime_agent':('sdk_capability_read','runtime_snapshot_read','worker_state_read','connection_holders_read','request_cancellation_read','sdk_snapshot_read','sdk_interfaces_read','sdk_health_read'),
    'lifecycle_agent':('process_event_read',),
    # ★ 运算工具与 `raw_read` 同一族权限：**能读这份数据的人才能算它**
    "metrics_agent": ("promql_read", "raw_read", "series_stats", "series_rate",
                      "series_change", "series_slope", "series_compare", "math_eval"),
    "log_agent": ("logql_read",),
    "trace_agent": ("traceql_read", "trace_read"),
    "change_agent": ("change_read",),
    "topology_agent": ("topology_read",),
    "memory_agent": ("memory_read",),
    "qa_agent": ("kb_search", "kb_document_read"),
    # ↓ 后置角色：只吃"证据"，不碰原始遥测
    "rca_agent": ("topology_read", "memory_read"),
    "critic_agent": ("evidence_lookup",),
    "remediation_agent": ("change_write",),
    "report_agent": (),
}

#: 明确降级工具；未配置知识源由 handler 如实返回不可用。
DEGRADED_TOOLS_P1: Final[tuple[str, ...]] = ()
assert set(AGENT_TOOL_MATRIX) == set(AGENT_ROLES), (
    "AGENT_TOOL_MATRIX 未覆盖全部角色（漏掉的角色会 fail-open：查表查不到就当没权限才对）"
)
DIAGNOSTIC_TOOL_NAMES: Final[frozenset[str]] = frozenset(t for role in
    ('database_agent','runtime_agent','lifecycle_agent','sdk_access_agent') for t in AGENT_TOOL_MATRIX[role])
for _role, _tools in AGENT_TOOL_MATRIX.items():
    _unknown = [t for t in _tools if t not in TOOL_NAME_SET and t not in DIAGNOSTIC_TOOL_NAMES]
    assert not _unknown, f"{_role} 被授予了未定义的工具 {_unknown}"

# ★ 红线 1：写工具只属于 remediation_agent
for _role, _tools in AGENT_TOOL_MATRIX.items():
    if _role != "remediation_agent":
        assert "change_write" not in _tools, (
            f"{_role} 被授予了 change_write —— 写能力只能由处置角色持有"
        )
assert AGENT_TOOL_MATRIX["remediation_agent"] == ("change_write",), (
    "remediation_agent 应当**只**持有 change_write —— "
    "它要读证据请走黑板，不要给它读工具（否则它可能自己下结论再执行）"
)

# ★ 红线 2：evidence_lookup 只属于 critic_agent（受限反查）
for _role, _tools in AGENT_TOOL_MATRIX.items():
    if _role != "critic_agent":
        assert "evidence_lookup" not in _tools, (
            f"{_role} 被授予了 evidence_lookup —— 它只该由 Critic 用来反查黑板"
        )

# ★ 红线 3：RCA / Critic 不得直接读原始遥测
#   ★ `raw_read` 也算在内：它读的是**同一批原始数据**的本地副本 ——
#     只因为是本地文件就放行，等于绕开"后置角色只看黑板证据"这条约束。
_RAW_TELEMETRY: Final[tuple[str, ...]] = (
    "promql_read", "logql_read", "traceql_read", "raw_read",
    *DIAGNOSTIC_TOOL_NAMES,
)
for _role in ("rca_agent", "critic_agent"):
    _leak = [t for t in AGENT_TOOL_MATRIX[_role] if t in _RAW_TELEMETRY]
    assert not _leak, (
        f"{_role} 拿到了原始遥测工具 {_leak} —— "
        f"（RCA/Critic 只允许看黑板上的结构化证据）"
    )

# ★ 覆盖性：除降级项外，每个已定义工具都得有人能调 —— 否则它就是死代码
_granted: set[str] = {t for _tools in AGENT_TOOL_MATRIX.values() for t in _tools}
_orphan = sorted(TOOL_NAME_SET - _granted - set(DEGRADED_TOOLS_P1))
assert not _orphan, (
    f"以下工具没有任何角色能调用（也没登记为降级）：{_orphan} —— "
    f"要么授权，要么写进 DEGRADED_TOOLS_P1"
)


# ==================================================================
#  预计算的集合（便于测试与校验，避免各处重复构造）
# ==================================================================
EVIDENCE_SOURCE_SET: Final[FrozenSet[str]] = as_str_set(EvidenceSource)
INTENT_SET: Final[FrozenSet[str]] = as_str_set(Intent)
RISK_LEVEL_SET: Final[FrozenSet[str]] = as_str_set(RiskLevel)
ERROR_CODE_SET: Final[FrozenSet[str]] = as_str_set(ErrorCode)
