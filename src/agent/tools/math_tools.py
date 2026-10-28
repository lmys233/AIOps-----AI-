"""**数据运算工具**：对本地副本里的序列做数学运算（契约修订 21）。

★★ 为什么必须有这一层（用户 2026-10-03 的方案）：
> "查指标意图…AI 拿到数据后大概率要做平均、加权等数学运算。那 AIOps 先写一些数据运算的
>  tool，然后 AI 拿到数据后，把这些 tool list 注入提示词，让 AI 自己分析该怎么计算这些数据，
>  自己 tool call 计算好数据后，返回结构化回答就行。"

三条设计纪律：

1. **数字只能来自工具**：模型负责"选哪个算法、怎么解释"，**不负责算**。
   让模型心算 = 允许它编数字（`系列里 4 个点`也能算出一个漂亮但不存在的均值）。
2. **只读本地副本**：数据来自 `raw_read` 落盘的原始载荷（`rawstore.load_raw(raw_ref)`），
   **一律不重发上游请求** —— 与 `raw_read` 同一条纪律（"取数"与"算数"分开）。
3. **取不到就是取不到**：`raw_ref` 找不到 → 报"找不到"（`NOT_FOUND`），**不是 0**、也不是空值；
   序列里没有有限点 → 报"没有可算的点"。这条是"没查到 ≠ 0"红线的同族。

工具清单（每个都是纯函数：输入 `raw_ref`/数值，输出结构化数字）：

| 工具 | 算什么 | 典型问法 |
|---|---|---|
| `series_stats` | 计数/最小/最大/均值/中位/首值/末值/标准差 | "现在是多少" |
| `series_rate` | 每秒速率 + 窗口内增量（counter → 速率） | "QPS 是多少" |
| `series_change` | 末值 vs 首值：绝对差 / 百分比 / 方向 | "涨了多少" |
| `series_slope` | 线性趋势斜率 + R² | "趋势如何" |
| `series_compare` | 两条序列对比（同环比） | "比刚才高吗" |
| `math_eval` | 白名单算术：加权平均 / 求和 / 比值 / 百分比变化 | "加权平均是多少" |
"""

from __future__ import annotations

import math
import json
from typing import Any, Final, Mapping, Sequence


__all__ = ["MATH_TOOL_NAMES", "MATH_HANDLERS", "MATH_SCHEMAS", "serie_points_of"]

#: 参与运算的**最多点数**与**最多序列条数**（运算要快，且结果要可读）
MAX_POINTS_FOR_MATH: Final[int] = 20_000
MAX_SERIES_FOR_MATH: Final[int] = 50


class MathInputError(Exception):
    """入参/数据形状问题（**"算不了"与"算出来是 0"必须分开**）。"""


def _load_series(raw_ref: str) -> list[dict[str, Any]]:
    """从本地副本取序列；找不到 → 抛（**不返回空列表冒充"没有数据"**）。

    ★ 试两个键：证据里带的引用与**落盘键**可能差一个"取数局限"后缀
      （`raw_read` 用的是补全后的键）—— 只试一个会让"引用明明在"却报找不到。
    """
    from .rawstore import resolve_raw_ref  # noqa: PLC0415 —— 与回取共用同一套解析

    resolved = resolve_raw_ref(str(raw_ref or ""))
    doc = resolved[1] if resolved else None
    if not isinstance(doc, Mapping):
        raise MathInputError(f"本地副本里没有 {str(raw_ref)[:80]!r} —— "
                             f"这次**没取到**这份数据（不是「值是 0」）")
    data = doc.get("data")
    if not isinstance(data, list):
        raise MathInputError("本地副本里的载荷不是序列列表（形状不对）")
    return [item for item in data if isinstance(item, Mapping)][:MAX_SERIES_FOR_MATH]


def serie_points_of(item: Mapping[str, Any], *, limit: int = MAX_POINTS_FOR_MATH
                    ) -> list[tuple[float, float]]:
    """一条序列的 `[(时间, 值)]`，**只留有限值**（NaN/Inf 不是数，不能进平均）。"""
    out: list[tuple[float, float]] = []
    for pair in (item.get("points") or [])[:limit]:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        try:
            ts, value = float(pair[0]), float(pair[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(ts) and math.isfinite(value):
            out.append((ts, value))
    out.sort(key=lambda pair: pair[0])
    return out


def _first_series(raw_ref: str, metric: str = "") -> tuple[dict[str, Any], list[tuple[float, float]]]:
    """取**要算的那一条**：给了 `metric` 就按标签里的 `service`/名字选，否则第一条。"""
    series = _load_series(raw_ref)
    if not series:
        raise MathInputError("本地副本里没有任何序列")
    want = str(metric or "").strip()
    if ' | series=' in raw_ref:
        selector=raw_ref.split(' | series=',1)[1]
        from ..queries.selectors import brace_end,matchers
        try:
            selector=selector[:brace_end(selector,0)]
            try:labels=json.loads(selector)
            except json.JSONDecodeError:
                entries=matchers(selector[1:-1])
                if any(op!='=' for _,op,_ in entries):raise ValueError('only exact labels')
                labels={key:value for key,_,value in entries}
            if not isinstance(labels,dict) or not labels:raise ValueError('empty labels')
        except (ValueError,IndexError):raise MathInputError('序列引用标签无效') from None
        series=[item for item in series if all(str((item.get('metric') or {}).get(k,''))==str(v) for k,v in labels.items())]
    elif want and len(series)>1:
        series=[item for item in series if want in {str(v) for v in (item.get('metric') or {}).values()}]
    if len(series)!=1:
        raise MathInputError('序列引用未唯一匹配；需要明确服务和标签，不能默认取第一条')
    picked = series[0]
    points = serie_points_of(picked)
    if not points:
        raise MathInputError(f"这条序列里没有可算的点（{len(picked.get('points') or [])} 个原始点）")
    return picked, points


def _round(value: float, digits: int = 6) -> float:
    return round(float(value), digits)


def _rate_of(points: Sequence[tuple[float, float]]) -> dict[str, Any]:
    """首末差分 → 每秒速率与增量（counter 换算的标准口径）。"""
    (t0, v0), (t1, v1) = points[0], points[-1]
    span = t1 - t0
    delta = v1 - v0
    return {"span_seconds": _round(span, 3), "delta": _round(delta),
            "per_second": _round(delta / span, 9) if span > 0 else None,
            "first": _round(v0), "last": _round(v1), "points": len(points),
            # ★ 采样时刻（epoch 秒）：值的**新鲜度**靠它判断，不是靠"末点就是此刻"
            "first_ts": _round(t0, 3), "last_ts": _round(t1, 3)}


# ======================================================================
#  工具实现
# ======================================================================
async def series_stats(ctx: Any, params: Mapping[str, Any]) -> Any:
    """"现在是多少 / 一段时间的统计量"。"""
    _item, points = _first_series(str(params.get("raw_ref") or ""),
                                  str(params.get("metric") or ""))
    values = [value for _ts, value in points]
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = (ordered[middle] if len(ordered) % 2 else
              (ordered[middle - 1] + ordered[middle]) / 2)
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    payload = {"count": len(values), "min": _round(min(values)), "max": _round(max(values)),
               "mean": _round(mean), "median": _round(median),
               "first": _round(values[0]), "last": _round(values[-1]),
               "stddev": _round(math.sqrt(variance)),
               # ★ 采样时刻与跨度：让"这是什么时候的数"成为**可核对的事实**
               "first_ts": _round(points[0][0], 3), "last_ts": _round(points[-1][0], 3),
               "span_sec": _round(points[-1][0] - points[0][0], 3)}
    return _ok(ctx, "series_stats",
               f"{len(values)} 个点：末值 {payload['last']} / 均值 {payload['mean']}"
               f" / 最小 {payload['min']} / 最大 {payload['max']}", payload)


async def series_rate(ctx: Any, params: Mapping[str, Any]) -> Any:
    """"每秒多少"（counter → 速率）——"QPS 是多少"要的就是它。"""
    _item, points = _first_series(str(params.get("raw_ref") or ""),
                                  str(params.get("metric") or ""))
    payload = _rate_of(points)
    if payload["per_second"] is None:
        raise MathInputError("两点时间相同，算不出速率")
    return _ok(ctx, "series_rate",
               f"每秒 {payload['per_second']}（窗口 {payload['span_seconds']}s 内增量 "
               f"{payload['delta']}）", payload)


async def series_change(ctx: Any, params: Mapping[str, Any]) -> Any:
    """末值 vs 首值：涨跌多少、涨跌百分之几。"""
    _item, points = _first_series(str(params.get("raw_ref") or ""),
                                  str(params.get("metric") or ""))
    first, last = points[0][1], points[-1][1]
    pct = ((last - first) / abs(first) * 100.0) if first else None
    direction = "flat" if last == first else ("up" if last > first else "down")
    payload = {"first": _round(first), "last": _round(last), "delta": _round(last - first),
               "pct_change": _round(pct, 4) if pct is not None else None,
               "direction": direction, "points": len(points)}
    return _ok(ctx, "series_change",
               f"{direction}：{payload['delta']}"
               + (f"（{payload['pct_change']}%）" if pct is not None else "（首值为 0，百分比无意义）"),
               payload)


async def series_slope(ctx: Any, params: Mapping[str, Any]) -> Any:
    """最小二乘斜率 + R²：**"在涨还是在跌"要有趋势，不能只看首末两点**。"""
    _item, points = _first_series(str(params.get("raw_ref") or ""),
                                  str(params.get("metric") or ""))
    xs = [ts - points[0][0] for ts, _value in points]
    ys = [value for _ts, value in points]
    n = len(xs)
    if n < 2 or all(x == xs[0] for x in xs):
        raise MathInputError("点不足（或时间全相同），拟合不出趋势")
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx if sxx else 0.0
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot else 0.0
    payload = {"slope_per_second": _round(slope, 9), "r2": _round(r2, 4), "points": n,
               "direction": "flat" if slope == 0 else ("up" if slope > 0 else "down")}
    return _ok(ctx, "series_slope",
               f"趋势 {payload['direction']}（斜率 {payload['slope_per_second']}/秒，"
               f"拟合优度 R²={payload['r2']}）", payload)


async def series_compare(ctx: Any, params: Mapping[str, Any]) -> Any:
    """两条序列对比（同环比）：给**绝对差与百分比**，不给"好/坏"的判断。"""
    left_ref = str(params.get("left_ref") or params.get("raw_ref") or "")
    right_ref = str(params.get("right_ref") or "")
    if not right_ref:
        raise MathInputError("需要 left_ref 与 right_ref 两条序列")
    _l_item, left = _first_series(left_ref, str(params.get("metric") or ""))
    _r_item, right = _first_series(right_ref, str(params.get("metric") or ""))
    lv, rv = left[-1][1], right[-1][1]
    pct = ((lv - rv) / abs(rv) * 100.0) if rv else None
    payload = {"left_last": _round(lv), "right_last": _round(rv),
               "delta": _round(lv - rv),
               "pct_change": _round(pct, 4) if pct is not None else None}
    return _ok(ctx, "series_compare",
               f"左边 {payload['left_last']} vs 右边 {payload['right_last']}"
               f"（差 {payload['delta']}）", payload)


async def math_eval(ctx: Any, params: Mapping[str, Any]) -> Any:
    """白名单算术：`weighted_mean` / `sum` / `mean` / `ratio` / `pct_change`。

    ★ 不是表达式求值器（那等于给模型一个 `eval`）：只认这五个算子，参数是**数字列表**，
      于是"它算了什么"永远可核对。
    """
    op = str(params.get("op") or "").strip()
    values = _numbers(params.get("values"))
    if op == "weighted_mean":
        weights = _numbers(params.get("weights"))
        if len(values) != len(weights):
            raise MathInputError(f"加权平均要求 values 与 weights 等长（{len(values)} vs {len(weights)}）")
        total = sum(weights)
        if total == 0:
            raise MathInputError("权重之和为 0，算不出加权平均")
        payload = {"value": _round(sum(v * w for v, w in zip(values, weights)) / total),
                   "weights_sum": _round(total)}
    elif op == "sum":
        payload = {"value": _round(sum(values))}
    elif op == "mean":
        payload = {"value": _round(sum(values) / len(values))}
    elif op == "ratio":
        if len(values) != 2:
            raise MathInputError("比值要求恰好两个数（分子、分母）")
        if values[1] == 0:
            raise MathInputError("分母为 0，比值不存在（**不是 0**）")
        payload = {"value": _round(values[0] / values[1], 9)}
    elif op == "pct_change":
        if len(values) != 2:
            raise MathInputError("百分比变化要求恰好两个数（旧值、新值）")
        if values[0] == 0:
            raise MathInputError("旧值为 0，百分比变化不存在（**不是 0**）")
        payload = {"value": _round((values[1] - values[0]) / abs(values[0]) * 100.0, 4)}
    else:
        raise MathInputError(f"不认识算子 {op!r}（可选：weighted_mean/sum/mean/ratio/pct_change）")
    payload["op"] = op
    payload["count"] = len(values)
    return _ok(ctx, "math_eval", f"{op} = {payload['value']}（{len(values)} 个数）", payload)


def _numbers(raw: Any) -> list[float]:
    """把参数收成有限数列表（NaN/无穷/非数一律拒绝 —— 不能让它们静默进平均）。"""
    if not isinstance(raw, (list, tuple)):
        raise MathInputError("values/weights 必须是数字列表")
    out: list[float] = []
    for item in raw:
        try:
            number = float(item)
        except (TypeError, ValueError) as exc:
            raise MathInputError(f"{item!r} 不是数字") from exc
        if not math.isfinite(number):
            raise MathInputError(f"{item!r} 不是有限数")
        out.append(number)
    if not out:
        raise MathInputError("数字列表是空的")
    return out


def _ok(ctx: Any, tool: str, summary: str, payload: dict[str, Any]) -> Any:
    """成功产物：**人读的那句话进 `note`**（执行器会把它并进摘要），`data` 是算出来的数。"""
    from .sources import RawOutcome  # noqa: PLC0415 —— 避免模块级循环引用

    return RawOutcome(data=payload, raw_ref=f"math: {tool}", note=summary)


def _reject(message: str) -> None:
    """"算不了"要走**可自修**那条路（`TOOL_INVALID_PARAMS`），不是"数据源不可用"。

    ★ 为什么不是 `SourceUnavailable`：那会让 RCA 以为"这一路信号缺失"，
      进而**排除本来正确的假设**；而这里的真相是"你给的引用/算法不对"，
      正确处置是把原话喂回模型让它换一个引用/算法再试（`QueryRejected` 的语义）。
    """
    from .errors import QueryRejected  # noqa: PLC0415

    raise QueryRejected(message)


def _guard(fn: Any) -> Any:
    """把 `MathInputError` 翻成 `QueryRejected`（"算不了"≠"算出来是 0"）。"""
    async def _wrapped(ctx: Any, params: Mapping[str, Any]) -> Any:
        try:
            return await fn(ctx, params)
        except MathInputError as exc:
            _reject(str(exc))
    return _wrapped


#: 运算工具的入参 schema（**唯一来源**：注册表与提示词都读它）
_SCHEMA_SERIES: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "raw_ref": {"type": "string", "minLength": 1,
                    "description": "要算的那份本地数据的回查引用（取数工具结果里的 raw_ref）"},
        "metric": {"type": "string", "description": "（可选）按标签值选中某一条序列，如 order"},
    },
    "required": ["raw_ref"],
}
MATH_SCHEMAS: Final[dict[str, dict[str, Any]]] = {
    "series_stats": _SCHEMA_SERIES,
    "series_rate": _SCHEMA_SERIES,
    "series_change": _SCHEMA_SERIES,
    "series_slope": _SCHEMA_SERIES,
    "series_compare": {
        "type": "object",
        "properties": {"left_ref": {"type": "string", "minLength": 1},
                       "right_ref": {"type": "string", "minLength": 1},
                       "metric": {"type": "string"}},
        "required": ["left_ref", "right_ref"],
    },
    "math_eval": {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["weighted_mean", "sum", "mean", "ratio",
                                              "pct_change"]},
            "values": {"type": "array", "items": {"type": "number"},
                       "description": "参与运算的数字（来自其他工具的 result）"},
            "weights": {"type": "array", "items": {"type": "number"},
                        "description": "（仅 weighted_mean）与 values 等长的权重"},
        },
        "required": ["op", "values"],
    },
}

MATH_HANDLERS: Final[dict[str, Any]] = {
    "series_stats": _guard(series_stats), "series_rate": _guard(series_rate),
    "series_change": _guard(series_change), "series_slope": _guard(series_slope),
    "series_compare": _guard(series_compare), "math_eval": _guard(math_eval),
}
MATH_TOOL_NAMES: Final[tuple[str, ...]] = tuple(MATH_HANDLERS)

#: 这些工具**不会**发上游请求（判据用它，而不是"看代码觉得像"）
MATH_TOOLS_ARE_LOCAL: Final[bool] = True

