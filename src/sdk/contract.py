"""契约常量 · **唯一副本**。

★ 为什么这个模块必须存在：

  这里集中了全部"**改名即违约**"的字面量 ——
  11 个指标名、响应字段名、日志七字段、`change_events.diff` 的五个键、
  探针路径（`/health` `/ready` `/metrics` + 服务清单端点 `/services`）。它们被 SDK 的十几个模块引用，
  一旦各写一份，迟早出现"指标名在 metrics.py 里是 A、在别处是 B"。

  以前的实现就是因为这些字面量散落在 6 个文件里而漂移过。
  所以：**SDK 里其它任何模块都不允许手写这些字符串，一律 import 本模块。**
  同理，**没有人引用**的契约常量等于没接上：使用点必须 import 常量而不是手抄字面量，
  否则改名时不会红。

★ 覆盖范围与"语言无关"的关系：

  这些常量是**被观测系统的契约**（L0 层），本来与语言无关 ——
  Spring / Go 实现照抄同一份即可接入。
  本文件只是把那份契约在 Python 里**具名化**，方便校验器对着它检查。
"""

from __future__ import annotations

from typing import Final, Optional

__all__ = [
    "CONTRACT_VERSION",
    "PROBE_PATHS",
    "PROBE_HEALTH",
    "PROBE_READY",
    "PROBE_METRICS",
    "PROBE_SERVICES",
    "RESPONSE_FIELD_ELAPSED_MS",
    "RESPONSE_FIELD_SERVICE",
    "IDENTITY_FIELDS",
    "ENTITY_ID_PREFIX",
    "entity_id",
    "METRICS",
    "METRIC_NAMES",
    "LOG_FIELDS",
    "LOG_MSG",
    "LOG_TS",
    "LOG_LEVEL",
    "LOG_SERVICE",
    "LOG_TRACE_ID",
    "LOG_SPAN_ID",
    "LOG_ERROR_TYPE",
    "LOG_SEVERITY",
    "FAULT_TYPES",
    "CHANGE_DIFF_KEYS",
    "CHANGE_TABLE",
    "HTTP_BUCKETS",
    "ACQUIRE_BUCKETS",
]

# 接入契约版本：契约本身演进时 +1，便于以后定位"这条数据是哪版契约采的"
CONTRACT_VERSION: Final = "spec-2026.01"

# ------------------------------------------------------------------
#  实体 id —— ★ 集群名**可空**
#
#  `change_events.entity_id` 的格式要求是"同 `EntityRef.id`"，
#  所以 SDK 侧也需要这份前缀映射：**两侧各自实现同一份契约**
#  （与本文件里 `METRICS` 的做法一致 —— SDK 不 import Agent 的代码，
#   Agent 也不 import SDK 的；两者都对着同一张表）。
# ------------------------------------------------------------------
ENTITY_ID_PREFIX: Final[dict[str, str]] = {
    "service": "svc",
    "config": "cfg",
    "pod": "pod",
    "node": "node",
    "database": "db",
    "middleware": "mw",
}

#: `/health` 里自报的**身份字段**。
#: ★ 全部可空：取不到就报 `null`/`[]`，**SDK 不得自造默认值** ——
#:   造出来的值会让"未知"与"真的是这个值"再也分不开。
IDENTITY_FIELDS: Final[tuple[str, ...]] = ("cluster", "env", "namespace")


def entity_id(entity_type: str, name: str, cluster: Optional[str] = None) -> str:
    """构造规范实体 id：`svc:order@compose-cn-1`；**集群未知时省略 `@cluster`**。

    ★ 不发明占位值（`@unknown`）：占位值会让"没拿到集群名"与"真有个叫 unknown 的
      集群"再也分不开。
    """
    prefix = ENTITY_ID_PREFIX.get(entity_type, entity_type)
    clean = (cluster or "").strip()
    return f"{prefix}:{name}@{clean}" if clean else f"{prefix}:{name}"


# ------------------------------------------------------------------
#  探针路径（端点与方法契约）
# ------------------------------------------------------------------
PROBE_HEALTH: Final = "/health"
PROBE_READY: Final = "/ready"
PROBE_METRICS: Final = "/metrics"

# ★ 第 4 个探针：**服务清单端点**。
#   它回答"被观测系统当下有哪些服务活着"，让 Agent 侧不必自己维护服务清单，
#   也不必靠 `/ready` 的下游字段去猜拓扑（那是借用字段）。
#   ★ 并入 PROBE_PATHS 是有意的：中间件据此把它排除在**业务**指标与响应增强之外
#     —— 探针流量不是业务流量。
PROBE_SERVICES: Final = "/services"

PROBE_PATHS: Final = (PROBE_HEALTH, PROBE_READY, PROBE_METRICS, PROBE_SERVICES)

# ------------------------------------------------------------------
#  统一响应约定
#
#  ★ 这两个字段是**可观测性契约**，不是业务字段：
#    `_server_elapsed_ms` 是 client-server gap 的一半，缺了核心归因判据不成立。
# ------------------------------------------------------------------
RESPONSE_FIELD_ELAPSED_MS: Final = "_server_elapsed_ms"
RESPONSE_FIELD_SERVICE: Final = "service"

# ------------------------------------------------------------------
#  日志字段契约
#
#  ★ `msg` 这一条来自真实踩过的坑：
#    structlog 默认把正文叫 `event`，而采集端提取的是 `msg` ——
#    不显式重命名，Loki 里永远查不到正文。
# ------------------------------------------------------------------
LOG_FIELDS: Final = ("msg", "ts", "level", "service", "trace_id", "span_id", "error_type")
LOG_MSG: Final = "msg"
LOG_TS: Final = "ts"
LOG_LEVEL: Final = "level"
LOG_SERVICE: Final = "service"
LOG_TRACE_ID: Final = "trace_id"
LOG_SPAN_ID: Final = "span_id"
LOG_ERROR_TYPE: Final = "error_type"
# 错误明细的字段名：异常堆栈被压成单行后放进这个键
# （与业务错误响应用的 `detail` 同一个键，Log Agent 不必分辨两种形状）
LOG_SEVERITY: Final = "detail"

# ------------------------------------------------------------------
#  指标命名契约
#
#  结构：逻辑名 -> (指标名, 类型, 标签元组)
#  ★ 名字与标签**逐字**来自契约表，不得改写。
# ------------------------------------------------------------------
METRICS: Final[dict[str, tuple[str, str, tuple[str, ...]]]] = {
    "http_duration": (
        "http_request_duration_seconds", "Histogram",
        ("service", "method", "path", "status"),
    ),
    # 直方图自带 _count；单独列出来是为了强调"用它算 QPS/错误率"
    "http_duration_count": (
        "http_request_duration_seconds_count", "Counter",
        ("service", "method", "path", "status"),
    ),
    "pool_max": ("db_pool_max_connections", "Gauge", ("service",)),
    "pool_in_use": ("db_pool_in_use_connections", "Gauge", ("service",)),
    "pool_waiting": ("db_pool_waiting_connections", "Gauge", ("service",)),
    "pool_acquire": ("db_pool_acquire_seconds", "Histogram", ("service",)),
    "downstream_total": ("downstream_requests_total", "Counter", ("service", "target", "status")),
    "downstream_duration": ("downstream_duration_seconds", "Histogram", ("service", "target")),
    "server_self_reported": (
        "server_self_reported_duration_seconds", "Histogram", ("service", "endpoint"),
    ),
    "business_operations": (
        "business_operations_total", "Counter", ("service", "operation", "result"),
    ),
    "fault_injection": ("fault_injection_active", "Gauge", ("service", "fault_type")),
}

METRIC_NAMES: Final[tuple[str, ...]] = tuple(name for name, _, _ in METRICS.values())

# 故障注入旋钮名 —— 正常态恒 0，但 `fault_injection_active` 必须
# 按这些 `fault_type` 暴露，否则"值为 0"与"指标不存在"无法区分
FAULT_TYPES: Final[tuple[str, ...]] = (
    "DB_POOL_SIZE",
    "DB_POOL_ACQUIRE_HOLD_MS",
    "DB_POOL_TIMEOUT_SEC",
    "ARTIFICIAL_DELAY_MS",
    "ARTIFICIAL_ERROR_RATE",
)

# ------------------------------------------------------------------
#  直方图分桶：必须覆盖故障区间 5ms ~ 10s
#
#  ★ 只按"正常延迟"设桶（比如 1~50ms）是常见的错：
#    故障时所有样本都掉进 +Inf，P99 直接失去分辨力，
#    "慢了 10 倍"与"慢了 1000 倍"看起来一模一样。
# ------------------------------------------------------------------
HTTP_BUCKETS: Final[tuple[float, ...]] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)
# 获取连接通常比 HTTP 快一个量级，但饱和时会飙到秒级
ACQUIRE_BUCKETS: Final[tuple[float, ...]] = (
    0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 3.0, 10.0,
)

# ------------------------------------------------------------------
#  存储契约
# ------------------------------------------------------------------
CHANGE_TABLE: Final = "change_events"
# `diff` 是 JSONB，这五个键内嵌在里面（old/new 不是独立列）
CHANGE_DIFF_KEYS: Final = ("field", "old_value", "new_value", "action_hash", "scenario_id")
