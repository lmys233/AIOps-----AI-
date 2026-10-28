"""取数口径的**预算**（默认近 7 天 + 长度/点数阈值）。

★★ 为什么要有这个模块（两条实测约束，不是拍脑袋）：

  1. `7d @ step=30s` = 20160 点 → Prometheus 直接拒：
     `exceeded maximum resolution of 11,000 points per timeseries`。
     默认窗放大到 7 天后，**点数预算不是优化项，是前提**。
  2. `7d @ step=1h` 查 `rate[1m]` → **0 个非零点**（尖峰被从旁边跨过去）。
     ★ **只放大窗口 + 粗 step = 换个姿势继续看不见**；尖峰要靠窗口内聚合
     （见 `anomaly.py` 的 `WINDOW_*`）保命。

★ 与"默认值"的分工（**同一事实只留一份**）：

  · **默认窗口**（用户没说时用哪个）= `intent/slots.yaml::defaults.time_window`，**只有那一处**；
  · **本次预算**（最长看多久 / 最多取多少点 / step 最多放多大）= 本模块的常量。

  两者数值现在都是 7d，但**是两件事**：一个是"假设"，一个是"上限"。`assert_default_within_budget()`
  负责保证"配置的默认值不超过预算上限"（否则配置一改就静默超限）。
"""

from __future__ import annotations

import math
import re
from typing import Final, Optional

__all__ = [
    "BASE_STEP_SECONDS",
    "MAX_STEP_SECONDS",
    "MAX_QUERY_POINTS",
    "MAX_LOOKBACK_SECONDS",
    "MAX_DETAIL_SECONDS",
    "DEFAULT_SERIES_LIMIT",
    "MAX_SERIES_LIMIT",
    "DEFAULT_LOG_LIMIT",
    "MAX_LOG_LIMIT",
    "DEFAULT_TRACE_LIMIT",
    "MAX_TRACE_LIMIT",
    "duration_seconds",
    "step_seconds",
    "step_text",
    "points_for",
    "plan_step",
    "detail_truncation_note",
    "row_limit_text",
    "StepPlan",
    "assert_default_within_budget",
]

#: 基础采样步长（与既有口径一致：10s 抓取，30s 查询）
BASE_STEP_SECONDS: Final[int] = 30

#: step 最多放大到多少（再大就失去"看趋势"的意义；到这一步仍超限才**截断**）
MAX_STEP_SECONDS: Final[int] = 3600

#: 单条序列的点数上限（Prometheus 硬限 11000；留足余量，别贴着墙走）
MAX_QUERY_POINTS: Final[int] = 1500

#: 时间窗**长度**上限：7 天（用户指示"直接默认查询最近的七天记录"）
MAX_LOOKBACK_SECONDS: Final[int] = 7 * 86400

#: ★ 明细区间上限 = 点数上限 × 基础步长（1500 × 30s = **12.5h**）。
#:   超出部分**不召回明细**（太久远的大概率已被修复），由**聚合口径**覆盖整窗。
MAX_DETAIL_SECONDS: Final[int] = MAX_QUERY_POINTS * BASE_STEP_SECONDS

#: 三个遥测工具的**返回条数**口径（默认值 / 上限）。
#:
#: ★★ 为什么这些数要在这里、而不是各写在 schema 与取数端：工具的
#:   `description` 与结果 `note` 都要向模型交代"这次最多拿到多少条"，
#:   两份数字各写一处必然漂移 —— 漂移的那一天，模型看到的是**假话**。
DEFAULT_SERIES_LIMIT: Final[int] = 200
MAX_SERIES_LIMIT: Final[int] = 200
DEFAULT_LOG_LIMIT: Final[int] = 200
MAX_LOG_LIMIT: Final[int] = 500
DEFAULT_TRACE_LIMIT: Final[int] = 20
MAX_TRACE_LIMIT: Final[int] = 200

_UNIT_SECONDS: Final[dict[str, int]] = {"s": 1, "m": 60, "h": 3600, "d": 86400}

#: "秒 → 可读时长"的降序试除单位（`d` → `h` → `m`，只剩秒就写 `s`）
_READABLE_UNITS: Final[tuple[tuple[str, int], ...]] = (
    ("d", _UNIT_SECONDS["d"]), ("h", _UNIT_SECONDS["h"]), ("m", _UNIT_SECONDS["m"]),
)

_DURATION_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+)\s*([smhd])$", re.IGNORECASE)


def duration_seconds(text: str) -> Optional[int]:
    """把 `30m` / `7d` 这类时长归一成秒（认不出来 → `None`，**不猜**）。

    ★ 与 `intent/slots.py` 的规范化口径一致（`\\d+(s|m|h|d)`）——
      这里**不重写解析**，只做"契约里的合法写法"的换算。
    """
    matched = _DURATION_RE.match(str(text or "").strip())
    if matched is None:
        return None
    return int(matched.group(1)) * _UNIT_SECONDS[matched.group(2).lower()]


def _fit_step(window_seconds: float, points: int, base: int, max_step: int) -> int:
    """先把 step 抬到"点数装得下"，**向上取整到整分/整时**，再夹到 `[base, max_step]`。

    ★ 为什么要取整：`404s` 这种步长虽然合法，但读日志、跟人解释、和 Grafana 对齐都别扭；
      向上取整既好读，又只会让点数更少（不会超预算）。
    """
    need = math.ceil(window_seconds / max(1, points))
    if need > _UNIT_SECONDS["m"]:
        need = math.ceil(need / _UNIT_SECONDS["m"]) * _UNIT_SECONDS["m"]
    if need > _UNIT_SECONDS["h"]:
        need = math.ceil(need / _UNIT_SECONDS["h"]) * _UNIT_SECONDS["h"]
    return max(base, min(max_step, int(need)))


def step_seconds(window_seconds: float, *, points: int = MAX_QUERY_POINTS,
                 base: int = BASE_STEP_SECONDS, max_step: int = MAX_STEP_SECONDS) -> int:
    """为这个窗口选一个 step（秒）—— **先放大 step，别急着截断**。

    ★ 为什么不直接截断时间：用户要的是"查最近七天**里有没有出过事**"。
      把窗口截成"最近 4 小时"虽然也合法，但等于把需求做没了。
    """
    return _fit_step(max(0.0, float(window_seconds)), points, base, max_step)


def step_text(window_seconds: float, **kwargs: int) -> str:
    """`step` 的 Prometheus 文本写法（整分钟用 `m`、整小时用 `h`，否则 `s`）。"""
    value = step_seconds(window_seconds, **kwargs)
    for unit, size in _READABLE_UNITS:
        if value % size == 0:
            return f"{value // size}{unit}"
    return f"{value}s"


def points_for(window_seconds: float, step: int) -> int:
    """按 step 算这次会取回多少点（用于判断要不要截断）。"""
    return int(math.ceil(max(0.0, float(window_seconds)) / max(1, int(step)))) + 1


class StepPlan:
    """一次取数预算的**结论 + 留痕**（`truncated` / `span` 必须能被说出来）。

    ★★ 修正（用户追加指示）：**不放大 step，而是截短明细区间**。
      用户原话：「7d 窗，但是要**长度限制**啊，**限制到时间减少**……**最近发生的尽量获取到**，
      但是**太久远的大概率已经被修复了，所以不应该被召回**。」
      放大 step 会把**最近的数据一起变糙**（"最近的"与"久远的"同等对待），与这条要求相反。
    """

    __slots__ = ("step", "span", "window", "points", "truncated", "note")

    def __init__(self, *, step: int, span: float, window: float, points: int,
                 truncated: bool, note: str) -> None:
        self.step = step
        self.span = span               # ★ 明细实际覆盖的时长（秒）
        self.window = window           # 用户/槽位给的时间窗（秒）
        self.points = points
        self.truncated = truncated
        self.note = note

    def __repr__(self) -> str:            # pragma: no cover —— 只为调试可读
        return (f"StepPlan(step={self.step}s, span={self.span:.0f}s, "
                f"window={self.window:.0f}s, points={self.points}, "
                f"truncated={self.truncated})")


def plan_step(window_seconds: float, *, points: int = MAX_QUERY_POINTS,
              base: int = BASE_STEP_SECONDS,
              max_step: int = MAX_STEP_SECONDS) -> StepPlan:
    """把"这个窗口怎么取**明细**"一次算清：**步长不变，区间按最近优先截短**。

    ★ 为什么保留 `max_step` 形参：调用方（测试）用它验证"不会放大"。
    ★ `truncated=True` 时调用方**必须**：
      ① 把明细区间收到 `[end - span, end]`；② 走**聚合口径**覆盖整窗（次数/峰值）；
      ③ 把 `note` 写进留痕（不静默）。
    """
    window = max(0.0, float(window_seconds))
    span = min(window, points * base)
    step = min(base, max_step)                 # 恒等于基础步长（保留形参只为显式）
    truncated = span < window
    note = ""
    if truncated:
        note = (f"窗口 {_text(window)}，**明细只召回最近 {_text(span)}**"
                f"（{points} 点 × {base}s；更久远的按**聚合口径**给次数/峰值，"
                f"不召回明细 —— 太久远的大概率已被修复）")
    return StepPlan(step=step, span=span, window=window,
                    points=int(span / step) + 1, truncated=truncated, note=note)


def _text(seconds: float) -> str:
    """时长的可读写法（`12.5h` / `7d`）—— 只用于留痕文案。"""
    total = max(0.0, float(seconds))
    for unit, size in _READABLE_UNITS:
        if total >= size:
            value = total / size
            return f"{value:.1f}{unit}".replace(".0", "")
    return f"{total:.0f}s"


def detail_truncation_note(window_seconds: float) -> str:
    """这个窗口会不会丢了明细 —— 会丢就给出**一句能直接读的留痕**。

    ★ 两种口径共用它：结果 `note`（模型看到的那句）与 `raw_ref`（事后回查）。
      「窗口 ≤ 7d」是上限，「明细只召回最近 12.5h」是这个上限**真正落到哪里**
      —— 只写上者会让模型以为自己看到了整窗的明细。
    ★ 窗口没超明细区间时返回空串（**没事就别刷屏**：局限那几句已经写在工具
      `description` 与结果的聚合口径里，这里只在"真的丢了明细"时才吭声）。
    """
    plan = plan_step(window_seconds)
    return plan.note if plan.truncated else ""


def row_limit_text(limit: int, *, unit: str, source: str) -> str:
    """「只取最近 N 条」这句话（`source` 有分页语义时**必须带方向**）。"""
    tail = f"，{source}" if source else ""
    return f"最多取回最近 {int(limit)} {unit}{tail}"


def assert_default_within_budget(default_window: str) -> None:
    """★ 配置校验：**默认窗口不得超过长度上限**（否则配置一改就静默超限）。

    故意做成"抛错"而不是"悄悄夹住"：默认值超过预算说明**配置与预算打架**，
    那是设计问题，不该由取数层替它兜着。
    """
    seconds = duration_seconds(default_window)
    if seconds is None:
        raise ValueError(f"默认时间窗 {default_window!r} 不是合法的时长写法")
    if seconds > MAX_LOOKBACK_SECONDS:
        raise ValueError(
            f"默认时间窗 {default_window}（{seconds}s）超过长度上限 "
            f"{MAX_LOOKBACK_SECONDS}s —— 请改配置或放宽预算（两者不能各说一套）")
