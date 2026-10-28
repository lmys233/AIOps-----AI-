"""把工具返回的大结果裁成模型看得完的形状（**摘要化**）。

一次 `promql_read` 完全可以返回十万个数据点（一条 30s 步长 × 30 天的序列就 8.6 万点）。
把它塞进模型的上下文，后果不是"贵"，是**这一轮排查直接废掉**：
上下文被一堆数字占满，真正有用的那 3 个异常点反而被挤出去。

★ 每个数据类型有且只有一个"可比关键字"，裁剪就是"按它排序/归并 + 截断"，
  而**不是**所有类型都做数值 min/max：
  · `promql_read`：`(时间戳, 指标值)`，有数值 → 按**数值**分桶取 min/max（保上下包络）
  · `logql_read`：一行日志（文本，无值可比）→ 按**频次**聚簇 `(service, level, error_type)`、按 count 排序
  · `traceql_read`：一条 trace（有耗时）→ 按 `duration_ms`，最慢的优先
  · `change_read`：一行变更（无值可比）→ 按 `changed_at`，最近的优先
  · `topology_read`：一条依赖边 → 按 `(from,to)` 去重后截断
  · `memory_read`：一条相似案例 → 服务已排序，取前 10 + **必带元数据**
  · `evidence_lookup`：一条证据 → 证据本身很小，只截断

★ 共同形状（执行器据此统一处理，不需要知道用了哪种策略）::

    {"summary": "<一句人话>", <明细列表>: [...], "<x>_total": <规模>, "truncated": <bool>}

★ 被裁掉的部分**必须写进 `truncated`**：如果摘要看起来"就这么点数据"，
  归因时就会低估影响面 —— 这跟把"没查成"当成"没异常"是同一类错误。
"""

from __future__ import annotations

import json
from statistics import mean
from typing import Any, Callable, Final, Iterable, Mapping, Optional, Sequence

from ..memory.prune import PRUNE_HEAD, PRUNE_TAIL, TOOL_RESULT_LIMIT, prune_tool_result

__all__ = [
    "MAX_SUMMARY_POINTS",
    "MAX_RAW_CHARS",
    "SUMMARIZERS",
    "summarize_series",
    "summarize_logs",
    "summarize_traces",
    "summarize_raw_trace",
    "summarize_changes",
    "summarize_topology",
    "summarize_memory",
    "summarize_evidence",
    "summarize_raw",
    "summarize_records",
    "summarize_math",
    "get_summarizer",
]

#: 结果上限 50 点（**硬上限**，任何摘要器都不得突破）
MAX_SUMMARY_POINTS: Final[int] = 50

#: 回取原生数据时的**上下文配额**（字符）：给得比别的摘要宽（它本来就是"查原文"），
#: 但仍必须有界 —— 「工具结果先裁」是压缩前的固定动作，不是可选项。
#: ★ 规则本体只有一份（`memory.prune`）：超长 → 头 + `middle pruned` + 尾。
MAX_RAW_CHARS: Final[int] = TOOL_RESULT_LIMIT

#: 每个簇里保留几条样本（簇是"同一个错误模式"，样本只需要够看出来就行）
_SAMPLES_PER_CLUSTER: Final[int] = 2
#: 最多保留多少个簇（否则"聚类的数量"自己就把上下文撑爆了）
_MAX_CLUSTERS: Final[int] = 12


# ======================================================================
#  通用工具
# ======================================================================
def _allocate(counts: Sequence[int], budget: int) -> list[int]:
    """把 `budget` 按各序列长度**按比例**分配，且每条至少 2 个点（首尾）。

    ★ 为什么每条至少 2：只保留 1 个点的序列看不出**趋势**，
      而"涨了还是跌了"正是归因里最先要看的东西。

    ★★ **不变量（可测）：`sum(结果) <= budget` 恒成立。**
      早期版本在"序列数 > budget/2"时会退化成"每条 1 点"，总和**超过 budget** ——
      摘要自己就突破了 50 点上限（实测 300 条序列 → 300 点）。
      现在：给不了的序列直接给 **0**，由调用方记进 `dropped_series`（**不许静默超限**）。
    """
    if not counts or budget <= 0:
        return [0] * len(counts)
    n = len(counts)
    if sum(counts) <= budget:
        return list(counts)

    floor = budget // 2                     # 每条至少 2 点的前提下，最多能装几条
    if n > floor:
        # 预算装不下这么多序列：按长度排序，前 floor 条各 2 点，其余给 0（= 丢弃）
        keep = sorted(range(n), key=lambda i: counts[i], reverse=True)[:floor]
        out = [0] * n
        for index in keep:
            out[index] = 2
        leftover = budget - sum(out)
        longest = sorted(keep, key=lambda i: counts[i], reverse=True)
        cursor = 0
        while leftover > 0 and longest:
            index = longest[cursor % len(longest)]
            if out[index] < counts[index]:
                out[index] += 1
                leftover -= 1
            cursor += 1
            if cursor > len(longest) * 4:
                break
        return out

    base = [min(count, 2) for count in counts]
    remaining = budget - sum(base)
    if remaining <= 0:
        return base
    total = sum(counts)
    for index, count in enumerate(counts):
        share = int(remaining * (count / total)) if total else 0
        base[index] = min(count, base[index] + share)
    # 余数补给最长的序列（它信息量最大）
    leftover = budget - sum(base)
    order = sorted(range(len(counts)), key=lambda i: counts[i], reverse=True)
    cursor = 0
    while leftover > 0 and order:
        index = order[cursor % len(order)]
        if base[index] < counts[index]:
            base[index] += 1
            leftover -= 1
        cursor += 1
        if cursor > len(order) * 3 and all(base[i] >= counts[i] for i in order):
            break
    return base


def _downsample(points: Sequence[Sequence[float]], budget: int) -> list[list[float]]:
    """把一条序列压到 `budget` 个点，**尽量保住"形状"**。

    ★ 策略：**分桶取极值（min/max per bucket）**，不是等距抽样：
      · 等距抽样最直觉、也最危险 —— 尖峰落在采样点之间就消失了；
      · LTTB（三角形面积最大）目标是"看上去像"，**不保证**一个极值都不丢；
      · 分桶取 min/max 则保证尖峰必是它所在桶的极值，O(n)、无参数、可证明，
        也是监控领域（RRD/Grafana）的标准做法。

    做法：把序列切成 `(budget-2)//2` 个桶，每桶取**最小与最大**两个点；
    再强制并入**首点与末点**（趋势）。于是保留下来的东西是：

    · **首/末** → 整体涨跌（趋势）
    · **每桶的 min/max** → 每个时间段里的极端值（**毛刺一定在其中**）
    · 外加 `stats()` 给的规模与幅度、`peak` 给的全局峰值时刻

    ★ 为什么不是"等距 + 全局峰"（早期实现）：那只保得住**一个**尖峰。
      实测：双尖峰场景下，小的那个在三个不同位置**都被丢掉**。

    ★ `budget <= 3` 时退化处理（说明清楚，而不是悄悄变差）：
      · `budget == 1` → 只留**偏离均值最大**的那个点（单点表达不了趋势，那就表达异常）
      · `budget == 2` → 留 min 与 max（异常优先于首尾）
      · `budget == 3` → 首、末 + 偏离均值最大的点
    """
    clean = [list(p) for p in points if isinstance(p, (list, tuple)) and len(p) >= 2]
    if budget <= 0:
        return []
    if len(clean) <= budget:
        return clean

    values = [p[1] for p in clean]
    avg = mean(values)
    if budget == 1:
        return [clean[max(range(len(values)), key=lambda i: abs(values[i] - avg))]]
    first, last = 0, len(clean) - 1
    if budget == 2:
        imin = min(range(len(values)), key=lambda i: values[i])
        imax = max(range(len(values)), key=lambda i: values[i])
        if imin == imax:
            # ★ 整条序列是平的（min == max）：此时"异常优先"无从谈起，
            #   退回首尾 —— 否则一条平序列只留 1 个点，会打破"每条至少 2 点"的承诺。
            return [clean[first], clean[last]]
        return [clean[i] for i in sorted({imin, imax})]
    if budget == 3:
        peak = max(range(len(values)), key=lambda i: abs(values[i] - avg))
        return [clean[i] for i in sorted({first, last, peak})]

    buckets = max(1, (budget - 2) // 2)      # 留 2 个位置给首尾
    picked: set[int] = {first, last}
    for b in range(buckets):
        lo = b * len(clean) // buckets
        hi = (b + 1) * len(clean) // buckets
        if hi <= lo:
            continue
        span = range(lo, hi)
        picked.add(min(span, key=lambda i: values[i]))     # ★ 桶内最小
        picked.add(max(span, key=lambda i: values[i]))     # ★ 桶内最大
    if len(picked) > budget:
        # 只可能超出一点点（同一桶 min==max 时反而更少）：保住首尾 + 最偏离均值的点
        rest = sorted((i for i in picked if i not in (first, last)),
                      key=lambda i: abs(values[i] - avg), reverse=True)
        picked = {first, last} | set(rest[: max(0, budget - 2)])
    return [clean[i] for i in sorted(picked)]


def _stats(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {}
    first, last = values[0], values[-1]
    peak = max(values)
    trough = min(values)
    return {
        "count": len(values),
        "min": round(trough, 6),
        "max": round(peak, 6),
        "avg": round(mean(values), 6),
        "first": round(first, 6),
        "last": round(last, 6),
        "delta": round(last - first, 6),
        "trend": "up" if last > first else ("down" if last < first else "flat"),
    }


def _truncate(items: Sequence[Any], cap: int) -> tuple[list[Any], bool]:
    if len(items) <= cap:
        return list(items), False
    return list(items[:cap]), True


# ======================================================================
#  ① 时序（PromQL）
# ======================================================================
def _series_importance(points: Sequence[Sequence[float]]) -> float:
    """一条序列"值不值得看"：**偏离自身均值的最大幅度**（异常幅度）。

    ★ 为什么用它而不是"点数"或"最大绝对值"：
      归因关心的是"谁不正常"，不是"谁数据多"或"谁数值大"（QPS 天然比错误数大）。
    """
    values = [float(p[1]) for p in points if isinstance(p, (list, tuple)) and len(p) >= 2]
    if not values:
        return 0.0
    avg = mean(values)
    return max(abs(v - avg) for v in values)


def summarize_series(data: Any) -> dict[str, Any]:
    """指标序列 → 统计量 + 逐桶极值 + 趋势（**总点数硬上限 50**）。

    ★★ 两层裁剪，顺序不能反：

      1. **先裁序列**：预算 50 点、每条至少 2 点 → 最多装 25 条序列。
         装不下时按"异常幅度"（`_series_importance`）留下最值得看的，
         其余整条进 `dropped_series`（**带名字**，否则"少了谁"看不出来）。
      2. **再裁每条序列的点**：分桶取 min/max（`_downsample`），
         保证毛刺不被等距抽样跳过。

    ★ 早期实现只有第 2 层，于是"序列数 > 25"时会退化成每条 1 点、且**总点数突破 50**
      （实测 300 条 → 300 点）—— 那正好把"上下文不会爆"这条承诺打穿了。
    """
    if isinstance(data,Mapping) and data.get('kind')=='metric_catalog':
        from .metric_catalog import checked
        catalog=checked(dict(data))
        return {**catalog,'summary':f'{len(catalog["metrics"])} 项指标目录；complete={catalog["complete"]}；不是健康判断'}
    series = [s for s in (data or []) if isinstance(s, Mapping)]
    slots = max(1, MAX_SUMMARY_POINTS // 2)          # 每条至少 2 点 → 最多几条序列

    dropped_series: list[dict[str, Any]] = []
    if len(series) > slots:
        ranked = sorted(series, key=lambda s: _series_importance(s.get("points") or []),
                        reverse=True)
        kept_series = ranked[:slots]
        for item in ranked[slots:]:
            metric = dict(item.get("metric") or {})
            dropped_series.append({
                "metric": metric,
                "points": len(item.get("points") or []),
                "importance": round(_series_importance(item.get("points") or []), 6),
                "reason": (f"序列数（{len(series)}）超过预算（{slots} 条 × ≥2 点）；"
                           f"该序列异常幅度较小，整条未保留"),
            })
        series = kept_series

    counts = [len(s.get("points") or []) for s in series]
    total = sum(counts)
    # ★★ **规模事实**（顶层 `points_total`）必须按"取数端记下的原点数"算 ——
    #   取数层可能因点数上限截断过，只数留下来的那些会把影响面算小。
    total_raw = sum(max(len(s.get("points") or []), int(s.get("points_total") or 0))
                    for s in series)
    budgets = _allocate(counts, MAX_SUMMARY_POINTS)

    out: list[dict[str, Any]] = []
    truncated_points: list[str] = []
    for item, budget in zip(series, budgets):
        points = item.get("points") or []
        values = [float(p[1]) for p in points if isinstance(p, (list, tuple)) and len(p) >= 2]
        kept = _downsample(points, budget)
        # ★★ **规模事实优先取取数端记下的原点数** —— 取数层可能因点数上限截断过
        #   （`points_truncated`），此时 `len(points)` 只是"留下的那些"；
        #   影响面正是靠这个数折算的，丢了就会低估。
        raw_total = int(item.get("points_total") or 0)
        total_points = max(len(values), raw_total)
        if item.get("points_truncated") and raw_total > len(values):
            truncated_points.append(f"{total_points}→{len(values)}")
        entry: dict[str, Any] = {
            "metric": dict(item.get("metric") or {}),
            "stats": _stats(values),
            "points": kept,
            "points_total": total_points,
            "points_truncated": bool(item.get("points_truncated")),
        }
        if isinstance(entry["stats"], dict):
            # `count` 描述的是**被统计的样本**；规模事实另给 `count_total`（两者不许混）
            entry["stats"]["count_total"] = total_points
        if values:
            avg = mean(values)          # ★ 同 `_downsample`：均值先算，别塞进 key 里
            peak_index = max(range(len(values)), key=lambda i: abs(values[i] - avg))
            entry["peak"] = {
                "ts": points[peak_index][0],
                "value": round(values[peak_index], 6),
            }
        out.append(entry)

    kept_total = sum(len(entry["points"]) for entry in out)
    parts = [f"{len(out)} 条序列 / 共 {total} 点，保留 {kept_total} 点"
             f"（上限 {MAX_SUMMARY_POINTS}）"]
    if truncated_points:
        parts.append(f"★ 取数端按点数上限截断过（原→留：{'、'.join(truncated_points[:3])}）")
    if dropped_series:
        parts.append(f"★ 另有 {len(dropped_series)} 条序列因预算不足**整条未保留**")
    summary = "；".join(parts) if out else "查了，这个时间窗内没有任何序列"
    return {
        "summary": summary,
        "series": out,
        "series_count": len(out),
        "series_dropped": len(dropped_series),
        "dropped_series": dropped_series,
        # ★ `points_total` = **规模事实**（含取数端截断掉的那些）；
        #   `points_kept` = 真正进上下文的点数。两者不可混。
        "points_total": max(total, total_raw),
        "points_kept": kept_total,
        "truncated": (max(total, total_raw) > kept_total) or bool(dropped_series),
        "points_truncated": bool(truncated_points),
        "budget_per_series": list(budgets),
    }


# ======================================================================
#  ② 日志（LogQL）
# ======================================================================
def _parse_log_line(line: Any) -> dict[str, Any]:
    """把日志行解析成结构化字段；不是 JSON 就原样当 msg（**不丢内容**）。"""
    text = line if isinstance(line, str) else json.dumps(line, ensure_ascii=False)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {"msg": text[:200], "level": "", "error_type": ""}
    if not isinstance(parsed, Mapping):
        return {"msg": text[:200], "level": "", "error_type": ""}
    return {
        "msg": str(parsed.get("msg") or "")[:200],
        "level": str(parsed.get("level") or ""),
        "error_type": str(parsed.get("error_type") or ""),
        "trace_id": str(parsed.get("trace_id") or ""),
    }


def _ts_ns_int(raw: Any) -> Optional[int]:
    """`ts_ns` 归一成整数纳秒；拿不到返回 None（**不猜**）。"""
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def summarize_logs(data: Any) -> dict[str, Any]:
    """日志流 → 按"错误模式"聚类 + 首现时间 + 频次（样本数 ≤ 50）。

    ★★ `first_ts_ns` / `last_ts_ns` 必须按**时间数值**取 min/max，
      不能按"遍历到的第一条/最后一条"。

      原因：Loki 默认 `direction=backward`（从新到旧），所以"第一条遍历到的"
      其实是**最新**的那行。早期实现直接拿第一条当"首现时间"，于是
      Log Agent 报出来的"首现时间"是**最近一次出现的时间** —— 看起来有值、
      实际正好反了。按数值取 min/max 与 direction 无关，两种方向都对。
    """
    streams = [s for s in (data or []) if isinstance(s, Mapping)]
    clusters: dict[tuple[str, str, str], dict[str, Any]] = {}
    total_lines = 0

    for stream in streams:
        labels = dict(stream.get("stream") or {})
        service = str(labels.get("service") or "")
        for entry in stream.get("entries") or []:
            if not isinstance(entry, Mapping):
                continue
            total_lines += 1
            parsed = _parse_log_line(entry.get("line"))
            key = (service, parsed.get("level", ""), parsed.get("error_type") or parsed["msg"][:40])
            bucket = clusters.setdefault(key, {
                "service": service, "level": parsed.get("level", ""),
                "error_type": parsed.get("error_type", ""), "count": 0,
                "first_ts_ns": None, "last_ts_ns": None,
                "samples": [],
            })
            bucket["count"] += 1
            ts_ns = _ts_ns_int(entry.get("ts_ns"))
            if ts_ns is not None:
                first = _ts_ns_int(bucket["first_ts_ns"])
                last = _ts_ns_int(bucket["last_ts_ns"])
                if first is None or ts_ns < first:
                    bucket["first_ts_ns"] = str(ts_ns)
                if last is None or ts_ns > last:
                    bucket["last_ts_ns"] = str(ts_ns)
            if len(bucket["samples"]) < _SAMPLES_PER_CLUSTER:
                bucket["samples"].append({"ts_ns": entry.get("ts_ns"), **parsed})

    ordered = sorted(clusters.values(), key=lambda c: c["count"], reverse=True)
    kept, truncated = _truncate(ordered, _MAX_CLUSTERS)
    error_total = sum(c["count"] for c in clusters.values() if str(c.get("level")).upper() in ("ERROR", "FATAL"))
    summary = (
        f"{total_lines} 行 / {len(clusters)} 个模式（其中 ERROR/FATAL {error_total} 行），"
        f"保留前 {len(kept)} 个模式"
        if total_lines else "查了，这个时间窗内没有任何日志"
    )
    return {
        "summary": summary,
        "clusters": kept,
        "lines_total": total_lines,
        "clusters_total": len(clusters),
        "error_lines": error_total,
        "truncated": truncated or len(ordered) > len(kept),
    }


# ======================================================================
#  ③ 链路（TraceQL）
# ======================================================================
def summarize_traces(data: Any) -> dict[str, Any]:
    """trace 列表 → 最慢的若干条 + 服务分布 + 耗时统计（条数 ≤ 50）。"""
    traces = [t for t in (data or []) if isinstance(t, Mapping)]
    durations = [float(t["duration_ms"]) for t in traces
                 if isinstance(t.get("duration_ms"), (int, float))]
    by_service: dict[str, int] = {}
    for trace in traces:
        service = str(trace.get("root_service") or "unknown")
        by_service[service] = by_service.get(service, 0) + 1
    slowest = sorted(traces, key=lambda t: float(t.get("duration_ms") or 0), reverse=True)
    kept, truncated = _truncate(slowest, MAX_SUMMARY_POINTS)
    summary = (
        f"{len(traces)} 条 trace"
        + (f"，最慢 {max(durations):.0f}ms / 平均 {mean(durations):.0f}ms" if durations else "")
        if traces else "查了，这个时间窗内没有任何 trace"
    )
    return {
        "summary": summary,
        "traces": kept,
        "traces_total": len(traces),
        "duration_ms": _stats(durations),
        "by_root_service": by_service,
        "truncated": truncated,
    }


#: 一条 trace 里最多放进摘要的 span 数（下钻要的是"哪一跳断了"，不是整棵树）。
_MAX_SUMMARY_SPANS: Final[int] = 60


def summarize_raw_trace(data: Any) -> dict[str, Any]:
    """**一条** trace 的 span 树 → 保形 + 有界地放进上下文（`trace_read` 的摘要器）。

    ★ 与 `summarize_traces` 的分工：那个把"窗口里有哪些 trace"压成统计；
      这个把"这一条里每一跳长什么样"留下来 —— 断点判定要看 span 的父子与状态，
      统计摘要会把这两个字段抹掉。
    """
    if not isinstance(data, Mapping):
        return {"summary": "取数端没有返回可解析的 trace", "trace_id": "", "spans": [],
                "spans_total": 0, "truncated": False}
    spans = [s for s in (data.get("spans") or []) if isinstance(s, Mapping)]
    kept, truncated = _truncate(spans, _MAX_SUMMARY_SPANS)
    items = [{
        "span_id": s.get("span_id"), "parent_span_id": s.get("parent_span_id"),
        "service": s.get("service"), "name": s.get("name"), "kind": s.get("kind"),
        "status": s.get("status"), "duration_ms": s.get("duration_ms"),
        "start_ns": s.get("start_ns"),
        # ★ 属性只留"能说明断在哪条语句/路由"的那几个（全量属性会撑爆上下文）
        "attributes": {key: value for key, value in dict(s.get("attributes") or {}).items()
                       if key in _SPAN_ATTR_WHITELIST},
    } for s in kept]
    # ★ 报错的判据与断点判定**共用一份**（`trace_spans.ERROR_STATUS_CODES`）：
    #   真机的写法是 `status_code_error`，以前这里只认 `error`/`2` ——
    #   于是一条 6 个 error span 的 trace 被摘要成「其中报错 **0** 个」。
    #   ★ 局部导入：`attribution` 包初始化会反向走到本模块（循环导入），
    #     放在函数里只在真的算摘要时付这一次代价。
    from ..attribution.trace_spans import ERROR_STATUS_CODES  # noqa: PLC0415

    errors = [s for s in items
              if str(s.get("status") or "").strip().lower() in ERROR_STATUS_CODES]
    summary = (f"trace {data.get('trace_id') or '（无 id）'}：{len(spans)} 个 span"
               f"（保留 {len(items)}），其中报错 {len(errors)} 个"
               if spans else f"trace {data.get('trace_id') or '（无 id）'} 里没有 span")
    return {"summary": summary, "trace_id": data.get("trace_id"), "spans": items,
            "spans_total": len(spans), "error_spans": len(errors),
            "truncated": truncated or bool(data.get("truncated"))}


#: 进摘要的 span 属性白名单（判"断在哪条语句/路由"用得上，且**不含凭据**）。
_SPAN_ATTR_WHITELIST: Final[tuple[str, ...]] = (
    "db.statement", "db.system", "http.route", "http.method", "http.status_code",
    "rpc.method", "messaging.destination", "error.type", "error", "otel.status_code",
)


# ======================================================================
#  ④ 变更记录（change_read）
# ======================================================================
#: 变更时间的候选列名。★ **`changed_at` 排第一**：那是 `change_events` 的真实列名
#: （见 `deploy/postgres/init/01-schema.sql`），而且它要用来与异常 onset 对齐 ——
#: 取不到它，变更归因就失去了时间锚点。
#: 其余名字是兼容别名（不同实现/不同来源可能这么叫）。
_CHANGE_TIME_KEYS: Final[tuple[str, ...]] = ("changed_at", "occurred_at", "ts", "created_at")


def _change_time(row: Mapping[str, Any]) -> str:
    for key in _CHANGE_TIME_KEYS:
        value = row.get(key)
        if value:
            return str(value)
    return ""


def summarize_changes(data: Any) -> dict[str, Any]:
    """变更行 → 时间倒序 + diff 摘要（行数 ≤ 50）。

    ★ 排序键与输出字段都用**真实列名 `changed_at`**。
      早期版本只认 `occurred_at`/`ts`，于是：
      ① 排序键全是空串（顺序退化成"数据库返回什么就是什么"）；
      ② 输出里 `occurred_at` 恒为 `null` —— **变更时间在摘要里整个丢了**，
         而模型要靠它把"变更"和"指标拐点"对齐。
    """
    rows = [r for r in (data or []) if isinstance(r, Mapping)]
    ordered = sorted(rows, key=_change_time, reverse=True)
    kept, truncated = _truncate(ordered, MAX_SUMMARY_POINTS)
    items: list[dict[str, Any]] = []
    for row in kept:
        diff = row.get("diff")
        if isinstance(diff, str):
            try:
                diff = json.loads(diff)
            except ValueError:
                diff = {"_raw": diff[:200]}
        items.append({
            "change_id": row.get("change_id") or row.get("id"),
            # `entity_id` 是真实列名（形如 `service:order@cluster`）；
            # `service` 是兼容别名（有的实现只给服务名）
            "entity_id": row.get("entity_id"),
            "service": row.get("service") or row.get("service_name"),
            "change_type": row.get("change_type"),
            "operator": row.get("operator") or row.get("actor") or row.get("actor_kind"),
            "source": row.get("source"),
            "changed_at": _change_time(row) or None,
            "diff": diff if isinstance(diff, Mapping) else {},
            "summary": row.get("summary") or row.get("message") or "",
        })
    summary = (f"{len(rows)} 条变更，保留最近 {len(items)} 条"
               if rows else "查了，这个时间窗内没有变更记录")
    return {
        "summary": summary,
        "changes": items,
        "count_total": len(rows),
        "truncated": truncated,
    }


# ======================================================================
#  ⑤ 拓扑 / ⑥ 记忆 / ⑦ 证据反查 / ⑧ 通用
# ======================================================================
def summarize_topology(data: Any) -> dict[str, Any]:
    """依赖边 → 去重后的边列表（边数 ≤ 50）。"""
    edges = [e for e in (data or []) if isinstance(e, Mapping)]
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for edge in edges:
        key = (str(edge.get("from_service")), str(edge.get("to_service")))
        if key in seen:
            continue
        seen.add(key)
        unique.append({
            "from": key[0], "to": key[1],
            "call_type": edge.get("call_type", "http"),
            "criticality": edge.get("criticality", "medium"),
        })
    kept, truncated = _truncate(unique, MAX_SUMMARY_POINTS)
    return {
        "summary": f"{len(kept)} 条依赖边（去重后；原始 {len(edges)} 条）" if edges else "查了，没有依赖边",
        "edges": kept,
        "edges_total": len(unique),
        "truncated": truncated,
    }


def summarize_memory(data: Any) -> dict[str, Any]:
    """记忆命中 → **必须带元数据**（时间/验证方式/命中次数/是否被取代）。"""
    hits = [h for h in (data or []) if isinstance(h, Mapping)]
    kept, truncated = _truncate(hits, 10)
    items = [{
        "memory_id": h.get("memory_id"),
        "symptom": h.get("symptom_text") or h.get("symptom"),
        "root_cause": h.get("root_cause"),
        "similarity": h.get("similarity"),
        "age_days": h.get("age_days"),
        "verified": h.get("verification"),
        "hits": h.get("hit_count"),
        "superseded_by": h.get("superseded_by"),
    } for h in kept]
    return {
        "summary": f"{len(items)} 条相似案例" if items else "查了，没有相似案例",
        "hits": items,
        "count_total": len(hits),
        "truncated": truncated,
    }


def summarize_evidence(data: Any) -> dict[str, Any]:
    """反查黑板证据：原样返回（证据本身就是结构化的小对象）。"""
    items = [e for e in (data or []) if isinstance(e, Mapping)]
    kept, truncated = _truncate(items, MAX_SUMMARY_POINTS)
    return {
        "summary": f"命中 {len(kept)} 条证据" if kept else "黑板上没有这条证据",
        "evidence": kept,
        "count_total": len(items),
        "truncated": truncated,
    }


def summarize_raw(data: Any) -> dict[str, Any]:
    """回取到的**原生数据**：放进上下文前先按字符裁（头 + 标记 + 尾）。

    ★★ 为什么不能像别的摘要那样"只留前 N 条/点"：原文的价值恰恰在**细节**
      （一整段 JSON、一整行堆栈）。裁掉中间是"照抄 DSH 的机制"——
      头尾都要留给模型，中间明确标记被剪掉多少，**不许**假装这是全文。
    ★ 裁剪本身调 `prune_tool_result`（**这条规则只有那一份实现**）：回取这一路
      与别处的超长块必须给出**同一个**结果，否则模型看到两套"剪了多少"。
    """
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
    total = len(text)
    kept = prune_tool_result(text)
    if len(kept) != total:
        return {
            "summary": f"本地副本原文（{total} 字符，中间已裁：留头 {PRUNE_HEAD} + "
                       f"尾 {PRUNE_TAIL}）—— 要完整原文请用更小的 max_chars 分段回取",
            "raw": kept, "chars_total": total, "truncated": True,
        }
    return {"summary": f"本地副本原文（{total} 字符，完整）",
            "raw": kept, "chars_total": total, "truncated": False}


def summarize_math(data: Any) -> dict[str, Any]:
    """运算结果 → 摘要（**保形 + 有界**）。

    ★ 与遥测摘要器不同：这里的产物本来就是"算好的几个数"，不需要裁剪策略 ——
      要做的是**原样保形**（模型要靠这些数继续算/写答案），只把长度封顶。
    """
    if not isinstance(data, Mapping):
        return {"summary": json.dumps(data, ensure_ascii=False, default=str)[:MAX_RAW_CHARS]}
    parts = [f"{key}={value}" for key, value in list(data.items())[:10]]
    return {"summary": "、".join(parts)[:MAX_RAW_CHARS],
            **{str(k): v for k, v in list(data.items())[:10]}}


def summarize_records(data: Any) -> dict[str, Any]:
    """通用兜底：任何"一行一条"的结果都能被安全裁剪。"""
    if isinstance(data, Mapping):
        items = [dict(data)]
    elif isinstance(data, Iterable) and not isinstance(data, (str, bytes)):
        items = [dict(x) if isinstance(x, Mapping) else {"value": x} for x in data]
    else:
        items = [{"value": data}]
    kept, truncated = _truncate(items, MAX_SUMMARY_POINTS)
    return {
        "summary": f"{len(kept)} 条记录",
        "records": kept,
        "count_total": len(items),
        "truncated": truncated,
    }


# ======================================================================
#  登记表（`ToolSpec.result_summarizer` 只能引用这里的名字）
# ======================================================================
SUMMARIZERS: Final[dict[str, Callable[[Any], dict[str, Any]]]] = {
    "summarize_series": summarize_series,
    "summarize_logs": summarize_logs,
    "summarize_traces": summarize_traces,
    "summarize_raw_trace": summarize_raw_trace,
    "summarize_changes": summarize_changes,
    "summarize_topology": summarize_topology,
    "summarize_memory": summarize_memory,
    "summarize_evidence": summarize_evidence,
    "summarize_raw": summarize_raw,
    "summarize_records": summarize_records,
    "summarize_math": summarize_math,
}


def get_summarizer(name: str) -> Callable[[Any], dict[str, Any]]:
    """按名字取裁剪函数。**取不到就抛** —— 不许悄悄退化成"不裁剪"。"""
    try:
        from ..summary.agent import deterministic
        fn=SUMMARIZERS[name]
        if not getattr(fn,'summary_policy',None):SUMMARIZERS[name]=deterministic('tool_payload',name+'-v1')(fn)
        return SUMMARIZERS[name]
    except KeyError as exc:
        raise KeyError(
            f"未登记的 result_summarizer={name!r}（可选：{sorted(SUMMARIZERS)}）—— "
            f"结果必须摘要化，**没有「不裁剪」这个选项**"
        ) from exc
