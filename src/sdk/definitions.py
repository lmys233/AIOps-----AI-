"""指标定义与记录点：11 个指标的名字、类型、标签与直方图分桶。

★ 本文件是 **SDK 里唯一出现 `Histogram(` 的地方** —— 这不是巧合：

  命名与标签是契约的一部分，实现方不得自行改名。
  把 11 个指标集中在一个文件、名字与标签**全部从 `contract.py` 取**，
  才能保证"改名"这件事在代码里**没有第二个地方可做**。

★ 三条硬性约束（全部来自真实踩过的坑）在这里怎么落：

  - `waiting` 只数"未完成的等待者"：`adapters/pool_asyncpg.py`
    （开始等 +1 / 拿到或超时 -1），**与任何队列长度无关**；
  - 所有 Gauge 启动时**显式初始化**：本文件 `init_gauges()`，
    无条件 `set(0)`，**无池的服务也报 0**；
  - `path` 用**路由模板**：`middleware_http.py` 读 `scope["route"].path`。

  第 2 条尤其容易漏：prometheus_client 不 `set` 就不产生 series，
  于是"值为 0"与"指标不存在"无法区分，`== 0` 类告警**永不触发**。
  所以 `init_gauges()` 是**启动时无条件执行**的，不是"用到才建"。

★ 关于 `fault_injection_active`（本项目的已知偏差）

  契约要求业务系统支持 5 个环境变量旋钮，而本次重规划把业务侧的注入能力
  **去掉了** —— 业务只保留正常配置项，故障改由**外部手段**制造。

  于是这里如实报 0，并且：**如果外部真的设了旋钮，SDK 会告警一次**，
  说明"这个值被忽略了"。这比"悄悄报 0"好 —— 否则评测时会以为注入生效了。
"""

from __future__ import annotations

from typing import Final

from prometheus_client import Counter, Gauge, Histogram

from .contract import (
    ACQUIRE_BUCKETS,
    FAULT_TYPES,
    HTTP_BUCKETS,
    METRICS,
)

__all__ = [
    "HTTP_DURATION",
    "POOL_MAX",
    "POOL_IN_USE",
    "POOL_WAITING",
    "POOL_ACQUIRE",
    "DOWNSTREAM_TOTAL",
    "DOWNSTREAM_DURATION",
    "SERVER_SELF_REPORTED",
    "BUSINESS_OPERATIONS",
    "FAULT_INJECTION",
    "init_gauges",
    "record_pool_state",
    "record_fault_state",
]

# ----------------------------------------------------------------------
#  11 个指标
#
#  名字 / 标签一律从 contract.py 取（`METRICS[逻辑名] = (名字, 类型, 标签)`），
#  这里只负责"按类型实例化 + 给直方图分桶"。
# ----------------------------------------------------------------------
_NAME: Final = {k: v[0] for k, v in METRICS.items()}
_LABELS: Final = {k: v[2] for k, v in METRICS.items()}

HTTP_DURATION = Histogram(
    _NAME["http_duration"],
    "HTTP 请求耗时（服务端视角，秒）",
    _LABELS["http_duration"],
    buckets=HTTP_BUCKETS,
)

POOL_MAX = Gauge(_NAME["pool_max"], "连接池上限", _LABELS["pool_max"])
POOL_IN_USE = Gauge(_NAME["pool_in_use"], "连接池当前占用", _LABELS["pool_in_use"])
POOL_WAITING = Gauge(
    _NAME["pool_waiting"],
    "★ 正在等待连接的请求数（只统计未完成的等待者）",
    _LABELS["pool_waiting"],
)
POOL_ACQUIRE = Histogram(
    _NAME["pool_acquire"],
    "获取数据库连接的耗时（秒）",
    _LABELS["pool_acquire"],
    buckets=ACQUIRE_BUCKETS,
)

DOWNSTREAM_TOTAL = Counter(
    _NAME["downstream_total"], "下游调用计数", _LABELS["downstream_total"],
)
DOWNSTREAM_DURATION = Histogram(
    _NAME["downstream_duration"],
    "下游调用耗时（**客户端视角**，秒）—— 与对端自报耗时比对即得 client-server gap",
    _LABELS["downstream_duration"],
    buckets=HTTP_BUCKETS,
)

SERVER_SELF_REPORTED = Histogram(
    _NAME["server_self_reported"],
    "服务端自报的内部处理耗时（与响应体里的自报值**同源**）",
    _LABELS["server_self_reported"],
    buckets=HTTP_BUCKETS,
)

BUSINESS_OPERATIONS = Counter(
    _NAME["business_operations"], "业务操作结果", _LABELS["business_operations"],
)

FAULT_INJECTION = Gauge(
    _NAME["fault_injection"], "故障注入是否生效（正常态恒 0）", _LABELS["fault_injection"],
)


# ----------------------------------------------------------------------
#  ★ 所有 Gauge 启动时显式初始化
# ----------------------------------------------------------------------
def init_gauges(service: str, *, pool_max: int | None = None) -> None:
    """把本服务的所有 Gauge **无条件**初始化，使其一开始就有 series。

    `pool_max` 是本服务的连接池上限；`None` 表示**本服务没有池** ——
    此时仍然要 `set(0)`，而不是跳过。
    ★ "gateway 没有连接池"与"gateway 没上报这个指标"是两件事：
      报 0 才能让 `service=~".*"` 的聚合查询不出现空洞，
      也才能让"值为 0"与"指标不存在"保持可区分。
    """
    POOL_MAX.labels(service=service).set(pool_max if pool_max is not None else 0)
    POOL_IN_USE.labels(service=service).set(0)
    POOL_WAITING.labels(service=service).set(0)
    record_fault_state(service)


def record_fault_state(service: str) -> None:
    """按 5 个旋钮名逐个暴露 `fault_injection_active`，正常态恒 0。

    ★ 即使恒为 0 也必须**逐个 fault_type 都建 series**：
      否则 `fault_injection_active{fault_type="ARTIFICIAL_ERROR_RATE"} == 1`
      这类查询会"永远查不到"而不是"查到 0"，评测时无法区分
      "没注入"与"指标没上报"。
    """
    for fault_type in FAULT_TYPES:
        FAULT_INJECTION.labels(service=service, fault_type=fault_type).set(0)


def record_pool_state(service: str, *, total: int, idle: int, waiting: int) -> None:
    """把池的当前状态写成 Gauge（池适配器在借/还时调用）。"""
    POOL_IN_USE.labels(service=service).set(max(0, total - idle))
    POOL_WAITING.labels(service=service).set(max(0, waiting))
