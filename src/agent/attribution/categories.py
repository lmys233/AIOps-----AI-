"""故障类别骨架 —— 归因层与编排层**共用**的唯一一份类别口径。

--------------------------------------------------------------------------------
★ 为什么单独一个模块（而不是放在 orchestration 里）
--------------------------------------------------------------------------------

类别词表同时被两边用：**编排层**（临时的 `ProvisionalScorer`、定向二次取证选角色）
与**归因层**（RCA 生成候选、LR 表、仲裁）。如果它留在 `orchestration/hypotheses.py`，
那么 `attribution → orchestration.hypotheses` 与
`orchestration.orchestrator → attribution` 就会形成**循环导入**
（真踩到：import 期直接炸）。

提到两边都能依赖的"叶子"模块，是唯一不需要延迟导入的干净解法 ——
而且它天然满足"同一份口径只有一处"。

--------------------------------------------------------------------------------
★★ 信号词必须与**真实产出的信号名**对齐
--------------------------------------------------------------------------------

类别 → 信号词用**子串匹配** `Evidence.signal`。词表里写错一个名字，
那一类假设就永远聚不到证据（不报错、只是判不出来）。所以：

· 词表里的每个词都必须能命中 `signals.KNOWN_SIGNAL_NAMES` 里的真实名字；
· `_check_keywords()` 在 import 期断言这件事（写错就拒绝启动）。

真实名字的清单与产出点见 `signals.py`（指标类直接从 `agents/anomaly.py` 取，不手抄）。

--------------------------------------------------------------------------------
★ 8 大类怎么定的
--------------------------------------------------------------------------------

按"**哪一路证据主导归因**"划分（与 `tools/intent_eval_cases.yaml` 的评测分类同一套口径）。

  · `pool_contention`：`pool_waiting` / `pool_acquire_p95` / `pool_in_use`
    —— 连接池被打满/上限被调小
  · `downstream_latency`：`client_server_gap` / `trace_duration_client`
    —— 下游依赖变慢
  · `downstream_error`：`downstream_error_rate` —— 下游依赖报错/不可用
  · `self_slowness`：`trace_duration_server` / `http_latency_p95` —— 自身逻辑/GC/锁问题
  · `error_burst`：`log_error_burst` / `http_error_rate` —— 应用层错误爆发
  · `change_induced`：`change_event` —— 变更引入（必须早于异常 onset）
  · `capacity_surge`：`http_latency_p95`（暂无流量证据，见偏差）—— 容量/流量突变
  · `topology_spread`：`blast_radius` / `dependency_graph` —— 拓扑传播
"""

from __future__ import annotations

from typing import Final

from .signals import unknown_rules

__all__ = ["CATEGORY_SIGNALS", "CATEGORY_LABEL", "CATEGORY_AGENT_ROLE",
           "CATEGORY_UNIVERSE"]

#: 类别 → 该类的信号词表（子串匹配 `Evidence.signal`）
CATEGORY_SIGNALS: Final[dict[str, tuple[str, ...]]] = {
    "pool_contention": ("pool_waiting", "pool_in_use", "pool_acquire"),
    "downstream_latency": ("client_server_gap", "trace_duration_client"),
    "downstream_error": ("downstream_error_rate",),
    "self_slowness": ("trace_duration_server", "http_latency_p95"),
    "error_burst": ("log_error_burst", "http_error_rate"),
    "change_induced": ("change_event",),
    "capacity_surge": ("http_latency_p95",),
    "topology_spread": ("blast_radius", "dependency_graph"),
}

#: 类别 → 中文命名（陈述句/报告里用）
CATEGORY_LABEL: Final[dict[str, str]] = {
    "pool_contention": "连接池/资源争用",
    "downstream_latency": "下游依赖变慢",
    "downstream_error": "依赖报错/不可用",
    "self_slowness": "自身逻辑变慢",
    "error_burst": "应用层错误爆发",
    "change_induced": "变更引入",
    "capacity_surge": "容量/流量突变",
    "topology_spread": "拓扑传播/爆炸半径",
}

#: 类别 → 负责该类的取证角色（定向二次取证时**只叫这几个角色**）
CATEGORY_AGENT_ROLE: Final[dict[str, str]] = {
    "pool_contention": "metrics_agent",
    "downstream_latency": "trace_agent",
    "downstream_error": "metrics_agent",
    "self_slowness": "metrics_agent",
    "error_burst": "log_agent",
    "change_induced": "change_agent",
    "capacity_surge": "metrics_agent",
    "topology_spread": "topology_agent",
}

#: 类别全集（校验用：任何"类别 → 某物"的表都必须与它一一对应）
CATEGORY_UNIVERSE: Final[frozenset[str]] = frozenset(CATEGORY_SIGNALS)


def _check_keywords() -> None:
    """import 期断言：每个信号词都能命中一个**真实存在**的信号名。"""
    for category, keywords in CATEGORY_SIGNALS.items():
        unknown = unknown_rules(keywords)
        if unknown:
            raise AssertionError(
                f"{category} 的信号词 {unknown} 命中不了任何真实信号名 —— "
                f"这一类的假设将**永远聚不到证据**（真实名字见 attribution/signals.py）")


_check_keywords()
