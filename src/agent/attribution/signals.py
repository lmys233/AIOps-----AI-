"""真实信号词表 —— 取证层**实际会产出**的信号名（归因层的口径锚点）。

--------------------------------------------------------------------------------
★★ 为什么需要这个文件
--------------------------------------------------------------------------------

归因层的 LR 表与类别词表都是**按信号名匹配**的。而这些信号名是**取证层产出的**：
指标类来自 `agents/anomaly.py` 的 `METRIC_SIGNALS`，日志/变更/拓扑来自各 Agent 的字面量，
链路来自 `trace_duration_{server|client}` 的拼接。

如果归因层的词表与真实名字**对不上**，后果不是报错，而是：

· 某条 LR 规则**永远不命中**（表里写着 `server_duration`，实际叫 `trace_duration_server`）；
· 更糟：某条**必要条件**永不满足 → 那个类别的假设**永远无法被确认**
  （实际发生的：`downstream_latency` 与 `self_slowness` 一条都定不了）；
· 反过来，**没有任何必要条件**的类别（`topology_spread`）会仅凭"依赖图"就被确认 ——
  真链路实测过：一次只有 2 条证据的排查给出了 `topology_spread` 的"根因"。

★ 单测抓不到它，因为**测试里的信号名是照着表写的**（自洽但与流水线无关）。
  真链路一跑就露馅 —— 所以这里把"真实信号名"变成一个**可校验的事实**，
  并让 `likelihoods` 在 import 期断言每条规则都能命中其中之一（写错就拒绝启动）。
"""

from __future__ import annotations

from typing import Final, Optional

__all__ = ["KNOWN_SIGNAL_NAMES", "TRACE_DURATION_SIGNALS", "SIGNAL_SOURCE",
           "source_of", "is_known", "unknown_rules"]

#: 链路信号：`trace_agent` 用 `f"trace_duration_{kind}"` 拼出来的两类
TRACE_DURATION_SIGNALS: Final[tuple[str, ...]] = (
    "trace_duration_server", "trace_duration_client",
)


def _metric_signal_names() -> tuple[str, ...]:
    """指标类信号名 —— **直接从取证层的表里取**，不在这里抄一遍（否则又会漂移）。"""
    from ..agents.anomaly import METRIC_SIGNALS

    return tuple(signal.name for signal, _template in METRIC_SIGNALS)


#: 非指标的信号名（各自的产出点见注释）—— 归因层看不到产出代码，只能在这里登记
_NON_METRIC_SIGNALS: Final[tuple[str, ...]] = (
    "qps",                  # metrics_agent 的 aux 通道（写入黑板 qps_estimates，不产生证据）
    "metric_snapshot",      # ★ metrics_agent 的**现状快照**证据（METRIC_QUERY 轻链路）。
    #                           它**刻意不进 LR 表**：现状是"现在多少"，不是"异常了" ——
    #                           若与 `http_latency_p95` 这类原信号名同名/同子串，
    #                           `_necessary_terms`（不看方向）会误判"必要条件已满足"
    #                           而似然比那边（看方向）拿不到支持，正是那种自相矛盾。
    "log_error_burst",      # log_agent 的聚类证据
    "client_server_gap",    # trace_agent 的 client/server 差值
    "change_event",         # change_agent
    "dependency_graph",     # topology_agent
    "blast_radius",         # topology_agent
    "memory_similar_case",  # memory_agent（未实现，名字先登记）
)


def _known() -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        _metric_signal_names() + TRACE_DURATION_SIGNALS + _NON_METRIC_SIGNALS))


#: 全部已知信号名（归因层做匹配校验用）
KNOWN_SIGNAL_NAMES: Final[tuple[str, ...]] = _known()


#: 信号 → 数据源（`EvidenceSource` 的取值）。
#:
#: ★ 为什么必须有：Critic 判"必要条件缺失 = 强反证"之前，必须先问一句
#:   "这条信号**所在的源**这次查成了吗" —— 源挂了就没有这条证据，
#:   那是"**没查成**"而不是"没有异常"。没有这张表就无法区分这两件事，
#:   于是 Prometheus 一挂，`pool_contention`（可能就是真因）反而被"强反证"排除。
SIGNAL_SOURCE: Final[dict[str, str]] = {
    "pool_waiting": "metrics",
    "pool_in_use": "metrics",
    "pool_acquire_p95": "metrics",
    "http_latency_p95": "metrics",
    "http_error_rate": "metrics",
    "downstream_error_rate": "metrics",
    "fault_injection": "metrics",
    "qps": "metrics",
    "metric_snapshot": "metrics",     # ★ 现状快照（同上，属指标这一路）
    "log_error_burst": "logs",
    "trace_duration_server": "traces",
    "trace_duration_client": "traces",
    "client_server_gap": "traces",
    "change_event": "changes",
    "dependency_graph": "topology",
    "blast_radius": "topology",
    "memory_similar_case": "memory",
}


def source_of(keyword: str) -> Optional[str]:
    """这个（可能只是子串的）信号关键词属于哪个数据源；取不到就 `None`。"""
    text = str(keyword or "")
    for name, source in SIGNAL_SOURCE.items():
        if text in name:
            return source
    return None


def is_known(keyword: str) -> bool:
    """这个关键词能不能命中某个**真实存在**的信号名（子串匹配，与 LR 表同一口径）。"""
    text = str(keyword or "")
    return any(text in name for name in KNOWN_SIGNAL_NAMES)


def unknown_rules(keywords: object) -> list[str]:
    """筛出**永远不可能命中**的关键词（写错名字的规则）。"""
    if isinstance(keywords, str):
        candidates = [keywords]
    else:
        candidates = [str(item) for item in (keywords or ())]
    return sorted({item for item in candidates if item and not is_known(item)})
