"""「这算不算异常」的**唯一判定处**（取证 Agent 共用）。

--------------------------------------------------------------------------------
★ 为什么要单独一个模块
--------------------------------------------------------------------------------

5 个取证 Agent 都要回答同一个问题："我拿到的这批数据，够不够格当成一条证据？"
如果每个 Agent 各写一套判断，会出现三种后果：

| 各写各的 | 后果 |
|---|---|
| 阈值散落 | 同一个指标在 Metrics Agent 那里算异常，在别处不算 —— 没人说得清为什么 |
| 判据不可见 | `threshold` 埋在两个 `if` 之间，"为什么这条算异常"无法回答（结论要可追溯） |
| 全凭感觉 | "有点波动就报异常" → 证据里全是噪声，RCA 打分被稀释 |

所以这里把判据**显式化成一张表**（`Signal`），每一条都写清楚
「变化的**绝对下限** / **相对下限** / 单位」，判断逻辑只有一份。

--------------------------------------------------------------------------------
★ 三个刻意的设计选择
--------------------------------------------------------------------------------

1. **基线取窗口起点**（`points[0]`），不是"历史同期"。
       理由：还没有长期基线库；而"这个窗口开始时是多少"是本次排查**真的知道**的东西。
   不知道就说不缺（`baseline=None`），不编。

2. **先看首尾、再看极值**：首尾差异够大 → `up`/`down`（"一直没回来"）；
   否则若中间冲高又回落 → `spike`/`drop`（"闪了一下"）。
   时序因果分析关心的是"**什么时候开始变的**"，这个顺序能保证 onset 落在真正的拐点上。

3. **阈值是"值得看一眼"的门槛，不是"确诊"**。
       最终"是不是根因"由 RCA 打分裁决；取证阶段的职责是
   **不把可疑的东西漏掉、也不把噪声塞满黑板**。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Mapping, Optional, Sequence

from ..common.enums import EvidenceDirection

__all__ = [
    "Signal",
    "Detection",
    "METRIC_SIGNALS",
    "detect",
    "detect_series",
    "series_of",
    "point_time",
]


@dataclass(frozen=True)
class Signal:
    """一路信号 + 它的异常判据。"""

    name: str
    unit: str
    min_abs: float = 1.0        # 绝对变化下限（单位见 unit）
    ratio: float = 0.30         # 相对变化下限（基线 > 0 时才用）
    flag: bool = False          # True = "非 0 即异常"（如 fault_injection_active）

    def threshold(self, baseline: float) -> float:
        """这条信号在当前基线下"多大变化才值得看"。

        ★ 基数为 0 时**不能用比例**（0 的 30% 还是 0），只能退回绝对下限 ——
          这正是"连接池等待从 0 涨到 3"这种最典型的故障该被抓到的地方。
        """
        if self.flag:
            return self.min_abs
        if baseline > 0:
            return max(self.min_abs, abs(baseline) * self.ratio)
        return self.min_abs


@dataclass(frozen=True)
class Detection:
    """一次"判出来了"的结论（**可解释**：基线 / 幅度 / 拐点都留下）。"""

    direction: EvidenceDirection
    baseline: Optional[float]
    magnitude: float
    onset: datetime
    peak: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {"direction": self.direction.value, "baseline": self.baseline,
                "magnitude": round(self.magnitude, 6),
                "onset": self.onset.isoformat(),
                "peak": None if self.peak is None else round(self.peak, 6)}


# ======================================================================
#  指标 → 我们真正要盯的那几路信号
# ======================================================================
#: ★ 只列**和故障直接相关**的信号，不是把 11 个指标全查一遍：
#:   查得越多，噪声越多，"哪条重要"越说不清。归属到 qps 的指标单独走 QPS 通道。
METRIC_SIGNALS: Final[tuple[tuple[Signal, str], ...]] = (
    # (信号, PromQL 模板 —— `%s` 是服务名的正则)
    (Signal("pool_waiting", "条连接", min_abs=1.0, ratio=0.30),
     'db_pool_waiting_connections{service=~"%s"}'),
    (Signal("pool_in_use", "条连接", min_abs=2.0, ratio=0.50),
     'db_pool_in_use_connections{service=~"%s"}'),
    (Signal("pool_acquire_p95", "秒", min_abs=0.05, ratio=0.50),
     'histogram_quantile(0.95, sum by (le, service) '
     '(rate(db_pool_acquire_seconds_bucket{service=~"%s"}[1m])))'),
    (Signal("http_latency_p95", "秒", min_abs=0.05, ratio=0.30),
     'histogram_quantile(0.95, sum by (le, service) '
     '(rate(http_request_duration_seconds_bucket{service=~"%s"}[1m])))'),
    (Signal("http_error_rate", "次/秒", min_abs=0.2, ratio=0.50),
     'sum by (service) (rate(http_request_duration_seconds_count'
     '{service=~"%s", status=~"5.."}[1m]))'),
    (Signal("downstream_error_rate", "次/秒", min_abs=0.2, ratio=0.50),
     'sum by (service, target) (rate(downstream_requests_total'
     '{service=~"%s", status=~"5.."}[1m]))'),
    (Signal("fault_injection", "开", min_abs=0.5, flag=True),
     'fault_injection_active{service=~"%s"}'),
)

#: QPS 通道（**不产生证据**，只写黑板 `qps_estimates`，供 RCA 做"影响面"折算）
QPS_QUERY: Final[str] = ('sum by (service) '
                         '(rate(http_request_duration_seconds_count{service=~"%s"}[1m]))')


# ======================================================================
#  ★★ 长窗下的**窗口内聚合**（峰值 / 累计次数）
# ======================================================================
#: `rate(...)` 是**瞬时聚合**：窗口一长、step 被放大，它就会从 1 分钟的尖峰旁边跨过去。
#: 实测（与 `.run/window_error_probe.py` 同批）：
#:
#: | 查询 | 7 天窗结果 |
#: |---|---|
#: | `rate[1m]` @ step=1h | **0 个非零点**（尖峰被漏掉） |
#: | `rate[1m]` @ step=10m | 命中 1 点（eval 点恰好落在那一分钟内，**是运气**） |
#: | `max_over_time(rate[1m])[7d:1m]` | **0.0200 命中**（确定性） |
#: | `increase(...[7d])` | **2 次**（计数口径，确定性） |
#:
#: 所以长窗下必须换成"窗口内聚合"，否则**把默认窗放大到 7 天等于白改**。
#: 元组 = `(信号前缀, 计数器选择器模板, 峰值单位, 次数单位)`。
#:
#: ★★ 名字**刻意不含基础信号名**（`window_error_*` 而不是 `http_error_rate_window_*`）：
#:   归因层的必要条件判据是 **子串匹配**（`rca._necessary_terms`：`rule.signal in item.signal`），
#:   于是 `http_error_rate_window_count` 会让 `error_burst` 的"必要条件已满足"**凭空成立** ——
#:   正是修过的那种自相矛盾。前缀式命名让它可以被安全地当成一条**独立事实**。
WINDOW_AGGREGATES: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("window_error",
     'http_request_duration_seconds_count{service=~"%s", status=~"5.."}', "次/秒", "次"),
    ("window_dependency_error",
     'downstream_requests_total{service=~"%s", status=~"5.."}', "次/秒", "次"),
)

#: 峰值 / 次数的后缀
PEAK_SUFFIX: Final[str] = "_peak"
COUNT_SUFFIX: Final[str] = "_count"

def peak_query(selector: str, window: str, step: str) -> str:
    """窗口内**峰值**（PromQL 子查询：内层 `rate[1m]` 逐分钟求值，外层取最大）。"""
    return (f'max_over_time((sum by (service) (rate({selector}[1m])))'
            f'[{window}:{step}])')


def count_query(selector: str, window: str) -> str:
    """窗口内**累计次数**（计数器 `increase`，确定性，不受 step 影响）。"""
    return f'sum by (service) (increase({selector}[{window}]))'


#: 两个聚合信号的描述（**只为了给渲染一个单位**；不进 `METRIC_SIGNALS`、不进 LR 表）
#: ★★ 两条都接：
#:   · **累计次数** `increase[窗口]` —— 确定性、与 step 无关；
#:   · **窗口内峰值** `max_over_time(rate[1m])[窗口:step]` —— 子查询逐分钟求值，
#:     能采到"1 分钟的尖峰"（粗 step 的瞬时查询会从旁边跨过去，实测 0 个非零点）。
#: ★ 冷启动提醒（实测踩过）：服务刚重启时计数器 series 只有一两个样本，
#:   子查询**算不出值** → 这一路本轮**没有证据**（如实记 `empty`，不是坏了）；
#:   等采集攒够样本（约两个抓取周期）再问就有了。★ 别把冷启动误判成"峰值口径不可用"。
WINDOW_SIGNALS: Final[tuple[Signal, ...]] = tuple(
    Signal(f"{prefix}{suffix}", unit, min_abs=0.0, ratio=0.0)
    for prefix, _selector, peak_unit, count_unit in WINDOW_AGGREGATES
    for suffix, unit in ((PEAK_SUFFIX, peak_unit), (COUNT_SUFFIX, count_unit))
)


def window_signal_names() -> frozenset[str]:
    """窗口聚合的信号名（供渲染/测试引用，避免各处硬编码字符串）。"""
    return frozenset(signal.name for signal in WINDOW_SIGNALS)


def assert_window_names_disjoint() -> None:
    """★★ 导入期护栏：窗口聚合的信号名**不得包含**任何基础信号名（子串也不行）。

    ★ 为什么值得一条断言：归因层的必要条件判据是 `rule.signal in item.signal`
      （子串匹配）。一旦名字里带上 `http_error_rate`，`error_burst` 的必要条件就会
      被一条"窗口内出现过错误"的**独立事实**凭空满足 —— 同一类自相矛盾。
      宁可启动即炸，也不要静默地把判据喂饱。
    """
    base = [signal.name for signal, _ in METRIC_SIGNALS]
    for name in window_signal_names():
        for other in base:
            if other in name:
                raise ValueError(
                    f"窗口聚合信号名 {name!r} 里含基础信号名 {other!r} —— "
                    f"会被归因层的**子串匹配**当成该信号的证据（同一类自相矛盾），"
                    f"请改用不含基础信号名的前缀式命名")


assert_window_names_disjoint()


# ======================================================================
#  判定
# ======================================================================
def _values(points: Sequence[Any]) -> list[float]:
    return [float(p[1]) for p in points
            if isinstance(p, (list, tuple)) and len(p) >= 2]


def detect(points: Sequence[Any], signal: Signal) -> Optional[Detection]:
    """一批 `[ts, value]` 点 → 异常判定（不异常则 `None`）。

    ★ 返回 `None` **不是**"没查成"，而是"查了，这一路没看出异常" ——
      两者的区别由调用方（`AgentOutcome.empty` vs `.degraded`）承担。
    """
    values = _values(points)
    if len(values) < 2:
        return None

    baseline = values[0]
    threshold = signal.threshold(baseline)
    peak, trough = max(values), min(values)
    last = values[-1]

    direction: Optional[EvidenceDirection] = None
    magnitude = 0.0
    if signal.flag:
        if peak >= signal.min_abs:
            direction = EvidenceDirection.UP
            magnitude = peak - baseline
    elif last - baseline >= threshold:
        direction = EvidenceDirection.UP
        magnitude = last - baseline
    elif baseline - last >= threshold:
        direction = EvidenceDirection.DOWN
        magnitude = baseline - last
    elif peak - baseline >= threshold:
        direction = EvidenceDirection.SPIKE
        magnitude = peak - baseline
    elif baseline - trough >= threshold:
        direction = EvidenceDirection.DROP
        magnitude = baseline - trough

    if direction is None:
        return None

    onset_index = _onset_index(values, baseline, threshold)
    return Detection(
        direction=direction,
        baseline=baseline,
        magnitude=magnitude,
        onset=point_time(points[onset_index]),
        peak=(peak if direction in (EvidenceDirection.UP, EvidenceDirection.SPIKE)
              else trough),
    )


def _onset_index(values: Sequence[float], baseline: float, threshold: float) -> int:
    """拐点 = 第一个**明显偏离基线**的点（偏离达到阈值的一半就算开始变了）。"""
    mark = max(threshold * 0.5, 1e-9)
    for index, value in enumerate(values):
        if abs(value - baseline) >= mark:
            return index
    return 0


def series_of(data: Any) -> list[Mapping[str, Any]]:
    """摘要化结果里的序列列表（`summarize_series` 的形状）。"""
    if not isinstance(data, Mapping):
        return []
    items = data.get("series") or []
    return [item for item in items if isinstance(item, Mapping)]


def detect_series(entry: Mapping[str, Any], signal: Signal) -> Optional[Detection]:
    """一条序列的异常判定（图省事的地方都走这里，别再各写一遍）。"""
    return detect(entry.get("points") or [], signal)


def point_time(point: Any) -> datetime:
    """Prometheus/Loki 的时间戳（epoch 秒）→ aware datetime。"""
    raw = point[0] if isinstance(point, (list, tuple)) and point else 0
    try:
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
