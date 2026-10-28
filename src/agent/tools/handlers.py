"""命名工具的取数实现。参数、权限、限流与审计由执行器统一负责。
原始结果经 summarize.py 裁剪；handler 的契约：
  1. 正常返回 `RawOutcome`（**空的也要返回**，`data=[]` 就是"查了没异常"）；
  2. "没查成"抛 `SourceUnavailable`；
  3. "查询有问题"抛 `QueryRejected`（Prometheus/Loki 由适配器抛，handler 不吞）。

★ handler **不写 audit、不看权限、不管限流** —— 那三道保护在 `executor.py` 里**顺序执行**，
  放在这里就会变成"每个工具各写一遍"，迟早有一个漏掉（本项目把这种叫"守了但没守住"）。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Mapping, Optional

from ..common.contracts import ENTITY_ID_PREFIX, EntityType
from ..common.enums import ErrorCode
from ..common.query import detail_truncation_note
from .errors import QueryRejected, ToolRefused
from .cache import (CacheKey, memory_freshness_sec, memory_is_fresh,
                    memory_window_closed)
from .changes import CHANGE_SQL, change_read
from .notes import SinkScope, collect, note
from .rawstore import load_raw
from .semantics import parse_duration
from .sources import (
    DEFAULT_SERIES_LIMIT,
    LokiSource,
    PrometheusSource,
    RawOutcome,
    SourceUnavailable,
    SqlSource,
    TempoSource,
)
__all__ = ["ToolContext", "cached_call", "bucket_seconds", "persisted_raw",
           "memory_is_fresh", "HANDLERS", "handler_for"]


@dataclass
class ToolContext:
    """执行一次工具调用时能碰到的**所有外部依赖**。

    ★ 全部可注入、全部可缺省：缺了不是崩溃，而是**如实降级**
      （`SourceUnavailable` → `degraded=True`）。这样：
      · 离线单测可以精确构造"数据源挂了"与"数据源没数据"两条分支；
      · 只跑一半环境（比如没接 Tempo）时，Agent 仍然能工作，只是少一路信号。
    """

    prometheus: Optional[PrometheusSource] = None
    loki: Optional[LokiSource] = None
    tempo: Optional[TempoSource] = None
    sql: Optional[SqlSource] = None
    topology: Optional[Any] = None      # 实现 interfaces.TopologyStore
    memory: Optional[Any] = None        # 实现 MemoryService.recall
    board: Optional[Any] = None         # 实现 BoardStore.get_evidence(incident_id, evidence_id)
    #: ★ 黑板是**按 incident 组织**的，所以反查必须知道是哪一次排查。
    #:   缺了它只能如实说"没法反查"，**不能**说成"证据不存在"（那是两种完全不同的结论）。
    incident_id: Optional[str] = None
    #: ★★ 查询结果**本地缓存**（`None` = 不缓存）。用户指示："缓存查询记录到**本地**
    #:   （而不是 redis），**用时间戳作为键**" —— 它是**副本**，丢了只是慢一点。
    cache: Optional[Any] = None
    #: ★★ **查询数据记忆**（按会话作用域键控的索引）。取数前先问它：
    #:   盖住了就不发请求；只盖住前半段就**只补缺口**、再与旧段拼接。
    query_memory: Optional[Any] = None
    knowledge: Optional[Any] = None    # Platform knowledge, never a runtime telemetry source.

    # ★ 这里**没有** `notes` 字段（也刻意不加）：`ToolContext` 是**进程级共享**的
    #   （执行器跨会话复用），往它的列表里 append 会跨会话串味，而且那个列表没有出口
    #   （留痕等于没留）。取数留痕走 `notes.py` 的 ContextVar 槽。


#: 从查询语句的 `{...}` 选择器里取 `service`（PromQL / LogQL 同一写法）。
_QUERY_ENTITY_RE: Final[re.Pattern[str]] = re.compile(
    r"""\bservice\s*=~?\s*["']([^"']+)["']""")


def _entity_from_query(query: Any) -> str:
    """这条查询**打在哪个实体上** —— 从查询自己的 `service` label 里读。

    ★★ 为什么需要它：`signal` / `entity` 是**取数身份**（进查询记忆的索引，
      AI 靠它认"记忆里已有哪一段数据"）。把它们从 `params` 里读**永远是空**：
      工具 schema 是 `additionalProperties: false`，编排层不会往调用参数里塞字段，
      于是 `QueryRecord.signal/entity` 恒为空串 —— **索引信息静默丢失**。

    ★ 这里只取**能从请求本身核实**的那一半：查询语句里的 `service` label 是
      真实存在的事实（`service="order"` → `order`）。正则/多值（`=~"a|b"`）
      一律**原样返回文本**，不猜测、不拆分。取不到 → 空串（如实说"不知道"）。
    """
    matched = _QUERY_ENTITY_RE.search(str(query or ""))
    return matched.group(1).strip() if matched else ""


async def _promql_read(ctx: ToolContext, params: Mapping[str, Any]) -> RawOutcome:
    if ctx.prometheus is None:
        raise SourceUnavailable("NotConfigured", "Prometheus 未接入（config 缺少 AIOPS_PROMETHEUS_URL）")
    if params.get('operation')=='catalog':
        method=getattr(ctx.prometheus,'catalogue',None)
        if not callable(method):raise SourceUnavailable('CatalogUnsupported','该指标源没有目录能力；保留旧查询路径')
        result=await method(str(params['query']),start=float(params['start']),end=float(params['end']),
            limit=min(64,int(params.get('limit') or 64)),cursor=params.get('cursor'))
        from .metric_catalog import checked
        from ..observed.context import current
        from ..queries.selectors import matchers
        query=str(params['query']).strip()
        if not (query.startswith('{') and query.endswith('}')):raise QueryRejected('catalog requires one scoped selector')
        scope=current();allowed=await scope.registered() if scope else None
        fields=matchers(query[1:-1])
        selected=next(((op,value) for key,op,value in fields if key=='service'),None)
        if selected and allowed is not None:
            op,value=selected;allowed={s for s in allowed if s==value} if op=='=' else {s for s in allowed if re.fullmatch(value,s)}
        checked(result.data,allowed)
        return result
    if params.get('cursor'):raise QueryRejected('cursor is only valid for metric catalog operations')
    # ★ `limit` 修前**没传下去**（schema 声明了它，取数端却忽略）→
    #   "最多返回多少条序列"这条承诺是空的。现在真的接上了。
    # ★★ 窗口由 `cached_call` 给（**它可能只补缺口那一段** —— 见 `cached_call` 的三层判断）
    async def _fetch(start: float, end: float) -> RawOutcome:
        return await ctx.prometheus.query_range(
            str(params["query"]), start=start, end=end,
            step=str(params.get("step") or "30s"),
            limit=int(params.get("limit") or DEFAULT_SERIES_LIMIT),
        )

    return await cached_call(ctx, source="prometheus", query=str(params["query"]),
                             start=float(params["start"]), end=float(params["end"]),
                             step=str(params.get("step") or "30s"), fetch=_fetch,
                             # ★★ 取数身份是**独立实参**，不从 `params` 里读：
                             #   schema 是 `additionalProperties: false`（不许模型多给字段），
                             #   所以 `params["signal"]` 永远是空的 —— 记忆索引里那一栏
                             #   会静默丢掉。这里从**查询自己**把实体读出来（它是请求里
                             #   真实存在的事实，不是猜的）。
                             signal="",
                             entity=_entity_from_query(params.get("query")))


async def cached_call(ctx: ToolContext, *, source: str, query: str, start: float,
                      end: float, step: str, fetch: Any, signal: str = "",
                      entity: str = "") -> RawOutcome:
    """日志直接查完整请求窗口；指标仅在旧数据本体可用时复用或补缺口。

    部分覆盖时先读取并持有旧段，防止补查期间缓存过期。拼接不可用则完整回源。
    合并后的载荷按本次窗口保存，记忆索引与可读取的缓存本体保持一致。
    """
    if source == "loki":
        return await fetch(start, end)
    scope, memory, cache = _layers(ctx)
    now = time.time()
    plan = _memory_plan(ctx, memory, scope, source=source, query=query,
                        start=start, end=end, step=step, now=now)
    if plan.reused is not None:
        return plan.reused
    # ★★ `fetch` 必须**按传进去的窗口**取数（`fetch(start, end)`）——
    #   修前它是"零参闭包、内部用原始窗口"，于是"只补缺口"算出来了却仍然整段重取
    #   （真机/单测都会静默丢掉"只补缺口"的全部收益）。
    outcome = await _cached_fetch(ctx, cache=cache, source=source, query=query,
                                  start=plan.start, end=end, step=step, fetch=fetch)
    if plan.record is not None:
        merged = _stitch(cache, plan.record, outcome, source=source, step=step,
                         previous=plan.previous, start=start, end=end)
        if merged is None:
            note("旧数据无法拼接 → 按本次完整窗口重新取数，不把补查空结果当成整个窗口为空")
            outcome = await _cached_fetch(ctx, cache=cache, source=source, query=query,
                                           start=start, end=end, step=step, fetch=fetch)
        else:
            outcome = merged
            _store_merged(cache, source, query, start, end, step, outcome)
    _remember(ctx, memory, scope, source=source, query=query, step=step,
              start=start, end=end, now=now, outcome=outcome,
              signal=signal, entity=entity)
    return outcome


@dataclass(frozen=True)
class _MemoryPlan:
    """记忆这一层给取数的结论：**要不要发请求、从哪儿开始、复用哪一份**。"""

    start: float
    record: Optional[Any] = None
    reused: Optional[RawOutcome] = None
    previous: Optional[RawOutcome] = None


def _layers(ctx: ToolContext) -> tuple[Any, Any, Any]:
    """三层各自的入口对象：`(作用域, 记忆, 本地缓存)`（缺哪个就是 `None`）。"""
    from ..memory.queries import scope_of  # noqa: PLC0415

    return scope_of(), getattr(ctx, "query_memory", None), getattr(ctx, "cache", None)


def _memory_trace(line: str) -> None:
    """★ opt-in 诊断（`AIOPS_QUERY_MEMORY_DEBUG=1`）：**"记忆为什么没命中"不能靠猜**。

    与本地缓存的诊断同一手法 —— 我这次就是靠它把"桶太小 / TTL 太短"读出来的。
    """
    if str(os.environ.get("AIOPS_QUERY_MEMORY_DEBUG") or "") not in ("1", "true", "on"):
        return
    try:
        path = Path(os.environ.get("AIOPS_QUERY_CACHE_DIR")
                    or ".run/cache/queries") / "_memory_debug.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%H:%M:%S')} {line}\n")
    except OSError:
        return


def _memory_plan(ctx: ToolContext, memory: Any, scope: Any, *, source: str, query: str,
                 start: float, end: float, step: str, now: float) -> _MemoryPlan:
    """先问记忆：**盖住了且够新就直接用**；只盖住前半段就把起点收到缺口处。"""
    from ..memory.queries import coverage  # noqa: PLC0415

    if memory is None or not scope:
        _memory_trace(f"SKIP memory={memory is not None} scope={bool(scope)} query={query[:40]}")
        return _MemoryPlan(start=start)
    try:
        found = coverage(memory.records(scope), query=query, start=start, end=end,
                         now=now, step=step)
    except Exception as exc:            # noqa: BLE001 —— 记忆坏了不许影响取数（fail-open）
        note(f"查询记忆不可用（{type(exc).__name__}）→ 按无记忆处理")
        return _MemoryPlan(start=start)
    _memory_trace(f"state={found.state} start={start:.0f} end={end:.0f} "
                  f"gap={found.gap_start:.0f}~{found.gap_end:.0f} query={query[:40]}")
    if found.usable:
        return _reuse_or_refetch(ctx, found, source=source, step=step, now=now, start=start)
    if found.state in ("stale", "partial"):
        previous = _from_memory(getattr(ctx, "cache", None), found.record,
                                source=source, step=step)
        if previous is None or not isinstance(previous.data, list):
            note("记忆索引存在，但旧数据已过期、丢失或不可读 → 本次按完整窗口重新取数")
            return _MemoryPlan(start=start)
        note(f"只补缺口 {found.gap_start:.0f}~{found.gap_end:.0f}：{found.reason}")
        return _MemoryPlan(start=float(found.gap_start), record=found.record, previous=previous)
    return _MemoryPlan(start=start)


def _reuse_or_refetch(ctx: ToolContext, found: Any, *, source: str, step: str,
                      now: float, start: float) -> _MemoryPlan:
    """★★ 记忆盖住了整段时**还新才复用**（门槛只有一处：`cache.memory_is_fresh`）。

    ★ 判据是**时间**：① 闭合窗（`end` 已过去，数据不可变）→ 长期可复用；
      ② 开窗 → 仅当抓取时间距今 ≤ 门槛（默认 60s，允许 1 分钟脏数据）；
      ③ 否则**必须重新取数**，哪怕它盖住了整段 —— 旧数据不许冒充当前状态。
    """
    from ..memory.queries import describe  # noqa: PLC0415

    age = max(0.0, now - float(found.record.fetched_at or 0.0))
    limit = memory_freshness_sec()
    if not memory_is_fresh(found.record, now=now):
        _memory_trace(f"STALE-MEMORY age={age:.1f}s limit={limit:.0f}s")
        note(f"记忆覆盖了整段，但那一份抓取于 {age:.0f}s 前（超过 {limit:.0f}s 门槛）"
             f"→ **本次重新取数**：{found.reason}")
        return _MemoryPlan(start=start)
    reused = _from_memory(getattr(ctx, "cache", None), found.record, source=source, step=step)
    if reused is not None:
        note(f"用记忆里的数据，**未发请求**：{describe(found.record)}"
             f"（命中 {found.record.start:.0f}~{found.record.end:.0f}，"
             f"{_freshness_note(found.record, now=now, age=age, limit=limit)}）")
        return _MemoryPlan(start=found.record.start, record=found.record, reused=reused)
    # ★ 记忆说"有"，但数据本体（本地落盘）没了 → **不许装**：整段重取并说明
    note(f"记忆里有这段窗口的数据，但本体落盘已不在 → 本次整段重取"
         f"（{found.reason}）")
    return _MemoryPlan(start=start)


def _freshness_note(record: Any, *, now: float, age: float, limit: float) -> str:
    """复用的理由（闭合窗 / 开窗但够新）—— 不许把"闭合窗"说成"还在 60s 内"。"""
    if memory_window_closed(getattr(record, "end", None), now=now):
        return (f"闭合窗、数据不可变，原始抓取于 {float(record.fetched_at):.0f}，"
                f"距现在 {age:.0f}s")
    return f"开窗但原始抓取于 {float(record.fetched_at):.0f}，距现在 {age:.0f}s ≤ {limit:.0f}s"


def _remember(ctx: ToolContext, memory: Any, scope: Any, *, source: str, query: str,
              step: str, start: float, end: float, now: float, outcome: RawOutcome,
              signal: str, entity: str) -> None:
    """把这次取到的**窗口**记成一条索引（数据本体留在本地缓存的落盘里）。"""
    from ..memory.queries import QueryRecord  # noqa: PLC0415

    if memory is None or not scope or not _is_series_result(source, outcome):
        return
    try:
        memory.remember(scope, QueryRecord(
            signal=signal, entity=entity, source=source, query=query, step=step,
            start=start, end=end, fetched_at=now, point_count=_count_points(outcome),
            segments=((start, end),), raw_ref=outcome.raw_ref,
            summary=f"{signal or source}：{end - start:.0f}s 窗口"), replace=True)
    except Exception as exc:            # noqa: BLE001 —— 同上
        note(f"查询记忆写入失败（{type(exc).__name__}）→ 本次不记")


def _is_series_result(source: str, outcome: RawOutcome) -> bool:
    """只有"序列型"结果值得进查询记忆（trace / 变更不是"某窗口的时序数据"）。"""
    return source in ("prometheus", "loki") and bool(outcome.data)


def _count_points(outcome: RawOutcome) -> int:
    total = 0
    for item in outcome.data or ():
        if isinstance(item, dict):
            total += len(item.get("points") or item.get("entries") or ())
    return total


def _from_memory(cache: Any, record: Any, *, source: str, step: str) -> Optional[RawOutcome]:
    """从记忆的索引里**取回数据本体**（本地缓存的落盘）——取不到返回 `None`（**不装**）。"""
    if cache is None or record is None:
        return None
    bucket = bucket_seconds(step)
    key = CacheKey(source=source, query=record.query, start=record.start,
                   end=record.end, step=step).bucketed(bucket)
    try:
        hit = cache.get(key)
    except Exception:                   # noqa: BLE001
        return None
    payload = (hit or {}).get("payload") if isinstance(hit, dict) else None
    if not isinstance(payload, dict) or payload.get("data") is None:
        return None
    return RawOutcome(
        data=payload.get("data"),
        raw_ref=(f"{payload.get('raw_ref', '')} | ★ 来自**查询记忆**（原始抓取于 "
                 f"{record.fetched_at:.0f}）| {CacheKey.bucket_note(bucket)}"),
        note=str(payload.get("note") or ""))


def _stitch(cache: Any, record: Any, outcome: RawOutcome, *, source: str,
            step: str, previous: Optional[RawOutcome] = None,
            start: Optional[float] = None, end: Optional[float] = None) -> Optional[RawOutcome]:
    """把**补来的那一段**与记忆里的旧段**拼起来**（接缝写进留痕）。"""
    old = previous or _from_memory(cache, record, source=source, step=step)
    if old is None or not isinstance(old.data, list) or not isinstance(outcome.data, list):
        return None
    series = _merge_series(old.data, outcome.data)
    if start is not None and end is not None:
        series = [{**item, "points": [p for p in item.get("points", [])
                                      if start <= float(p[0]) <= end]} for item in series]
    return RawOutcome(
        data=series,
        raw_ref=(f"{outcome.raw_ref} | ★ 与记忆旧段**拼接**（旧段 "
                 f"{record.start:.0f}~{record.end:.0f}，接缝 {record.end:.0f}；"
                 f"两段 step 可能不同，**接缝处不算连续**）"),
        note=outcome.note)


def _store_merged(cache: Any, source: str, query: str, start: float, end: float,
                  step: str, outcome: RawOutcome) -> None:
    """保存完整的合并载荷，供下一轮与索引一起读取。"""
    if cache is None:
        return
    try:
        key = CacheKey(source, query, start, end, step).bucketed(bucket_seconds(step))
        cache.put(key, {"data": outcome.data, "raw_ref": outcome.raw_ref, "note": outcome.note},
                  open_window=not cache.is_closed(end))
    except Exception as exc:  # noqa: BLE001
        note(f"合并结果缓存写入失败（{type(exc).__name__}）→ 下一轮完整回源")


def _merge_series(older: list, newer: list) -> list:
    """按 label 把两批序列拼起来（同序列的点并入并排序；不同序列各自保留）。"""
    buckets: dict[str, dict] = {}
    for item in list(older) + list(newer):
        if not isinstance(item, dict):
            continue
        labels = dict(item.get("metric") or {})
        key = json.dumps(labels, sort_keys=True, ensure_ascii=False)
        bucket = buckets.setdefault(key, {"metric": labels, "points": [], "entries": []})
        bucket["points"].extend(item.get("points") or [])
        bucket["entries"].extend(item.get("entries") or [])
    out: list[dict] = []
    for bucket in buckets.values():
        merged: dict[float, float] = {}
        for pair in bucket["points"]:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                try:
                    merged[float(pair[0])] = float(pair[1])
                except (TypeError, ValueError):
                    continue
        entry: dict[str, Any] = {"metric": bucket["metric"],
                                 "points": [[ts, value]
                                            for ts, value in sorted(merged.items())]}
        if bucket["entries"]:
            entry["entries"] = bucket["entries"]
        out.append(entry)
    return out


async def _cached_fetch(ctx: ToolContext, *, cache: Any, source: str, query: str,
                        start: float, end: float, step: str, fetch: Any) -> RawOutcome:
    """★ 本地缓存那一层：键（分桶后）一致才命中；闭合窗长期复用、开窗给秒级 TTL。"""
    if cache is None:
        return await fetch(start, end)
    bucket = bucket_seconds(step)
    key = CacheKey(source=source, query=query, start=start, end=end,
                   step=step).bucketed(bucket)
    try:
        hit = cache.get(key)
    except Exception as exc:            # noqa: BLE001 —— 缓存坏了不许影响取数（fail-open）
        note(f"查询缓存不可用（{type(exc).__name__}）→ 本次直接查数据源")
        hit = None
    if isinstance(hit, dict) and isinstance(hit.get("payload"), dict):
        payload = hit["payload"]
        return RawOutcome(
            data=payload.get("data"),
            raw_ref=(f"{payload.get('raw_ref', '')} | {cache.note_of(hit)}"
                     f" | {CacheKey.bucket_note(bucket)}"),
            note=str(payload.get("note") or ""))
    outcome = await fetch(start, end)
    try:
        # ★ `open_window` 用**原始 end** 判定（分桶只影响键，不影响"抓取时窗口开着吗"）
        cache.put(key, {"data": outcome.data, "raw_ref": outcome.raw_ref,
                        "note": outcome.note},
                  open_window=not cache.is_closed(end))
    except Exception as exc:            # noqa: BLE001 —— 同上：写不进就下次再查
        note(f"查询缓存写入失败（{type(exc).__name__}）→ 本次不缓存")
    return outcome


def bucket_seconds(step: str) -> float:
    """时间桶（秒）：`AIOPS_QUERY_CACHE_BUCKET_SEC` 与 `step` 量级取**较大者**。

    ★ 为什么与 step 取 max：桶比步长还小没有意义（同一批数据会被切成多个键）。
    ★ 为什么默认 120s（`cache.DEFAULT_BUCKET_SEC`）：一轮排查要 20~60s，
      桶取 30s 时"再问一次"几乎必然跨桶 → **缓存等于没做**（真机实测撞到）。
    """
    from .cache import DEFAULT_BUCKET_SEC as _CACHE_BUCKET

    try:
        env = float(os.environ.get("AIOPS_QUERY_CACHE_BUCKET_SEC") or _CACHE_BUCKET)
    except ValueError:                     # 配错了不炸：退回默认
        env = float(_CACHE_BUCKET)
    # ★ 时长的解析**只有一处**（`semantics.parse_duration`）—— 这里不再自带
    #   正则与单位表：两份写法迟早对不上（大小写 / 空格 / 纯数字各认一半）。
    #   `step` 认不出来 → 按 1 秒算（桶仍由 `env` 兜住下界）。
    step_sec = float(parse_duration(step) or 1)
    return max(step_sec, env)


async def _logql_read(ctx: ToolContext, params: Mapping[str, Any]) -> RawOutcome:
    if ctx.loki is None:
        raise SourceUnavailable("NotConfigured", "Loki 未接入（config 缺少 AIOPS_LOKI_URL）")

    async def _fetch(start: float, end: float) -> RawOutcome:
        return await ctx.loki.query_range(
            str(params["query"]), start=start, end=end,
            limit=int(params.get("limit") or 200),
            direction=str(params.get("direction") or "backward"),
        )

    return await cached_call(ctx, source="loki", query=str(params["query"]),
                             start=float(params["start"]), end=float(params["end"]),
                             step="log", fetch=_fetch,
                             # ★ 与 promql 同一口径：身份是独立实参，实体从查询里读
                             signal="",
                             entity=_entity_from_query(params.get("query")))


async def _traceql_read(ctx: ToolContext, params: Mapping[str, Any]) -> RawOutcome:
    from ..observed.context import current
    if current():
        from ..observed.trace_archive import search
        return await search(params)
    if ctx.tempo is None:
        raise SourceUnavailable("NotConfigured", "Tempo 未接入（config 缺少 AIOPS_TEMPO_URL）")
    return await ctx.tempo.search(
        str(params["query"]), start=float(params["start"]), end=float(params["end"]),
        limit=int(params.get("limit") or 20),
    )


#: `change_events` 的读取列（**不写 SELECT ***：
#: `EntityRef.id` 的服务前缀（形如 `svc:order@prod-cn-1`）。
#: ★ 用缩写 `svc` 而不是 `service`：前缀映射的唯一来源是
#:   `agent.common.contracts.ENTITY_ID_PREFIX`，手写字符串迟早写错一个字母。
_ENTITY_PREFIX: Final[str] = ENTITY_ID_PREFIX[EntityType.SERVICE.value]

# ★★ `entity_id` 的匹配必须**同时容纳两种写法**：规范写法 `svc:order@compose-cn-1`
#   （同 `EntityRef.id`）与**裸服务名** `order`（被观测系统当前实际写入的形式）。
#   早期只拼 `service:{name}@{cluster}%` → 前缀错 + 不认裸名 → **永远查不到**，
#   而后果不是报错，是"这一路查了没异常" —— 把"有变更"误报成"没有变更"，
#   RCA 会据此**排除变更假设**。假阴性比查询失败危险得多，所以这里两种都匹配。
_CHANGE_SQL: Final[str] = CHANGE_SQL

#: 变更记录的实现已搬到 `changes.py`（handlers 顶到行数上限；只搬代码不改口径）
_change_read = change_read


async def _trace_read(ctx: ToolContext, params: Mapping[str, Any]) -> RawOutcome:
    """按 `trace_id` 取**整条** trace 的 span 树（错误 → 断点下钻的那一步）。

    ★ 与 `traceql_read` 的分工：那个查的是"窗口里有哪些 trace"（列表、摘要），
      这个查的是"**这一条** trace 内部垮在哪一跳"（span 树）。
      两者是不同粒度的问题，合成一个工具会让返回形状随查询文本变来变去。
    """
    from ..observed.context import current
    if current():
        from ..observed.trace_archive import fetch
        return await fetch(params)
    if ctx.tempo is None:
        raise SourceUnavailable("NotConfigured", "Tempo 未接入（config 缺少 AIOPS_TEMPO_URL）")
    return await ctx.tempo.fetch_trace(str(params["trace_id"]))


async def _topology_read(ctx: ToolContext, params: Mapping[str, Any]) -> RawOutcome:
    """依赖拓扑（实体与爆炸半径）。实现走 `TopologyStore` 接口。"""
    from ..observed.context import current
    if current():
        from ..observed.archive_handlers import topology
        return await topology(params)
    if ctx.topology is None:
        raise SourceUnavailable(
            "NotConfigured", "拓扑存储未接入（先跑 topology-migrate + topology-sync）")

    service_id = str(params["service"]).strip()
    cluster = str(params.get("cluster") or "").strip()
    if ":" not in service_id and cluster:
        # ★ 前缀必须取自 `ENTITY_ID_PREFIX`（契约前缀是 `svc`，不是 `service`）——
        #   手写字符串就是第二份真相，迟早对不上。
        #   ★ 而且**允许没有集群名**：单集群/未自报时不拼 `@cluster`，
        #     因为"集群名是限定符、服务名才是定位符"。
        service_id = f"{_ENTITY_PREFIX}:{service_id}@{cluster}"

    try:
        edges = await ctx.topology.get_dependencies(
            service_id,
            direction=str(params.get("direction") or "downstream"),
            depth=int(params.get("depth") or 1),
        )
    except QueryRejected:
        raise
    except Exception as exc:  # noqa: BLE001 —— 拓扑侧的异常要翻译成工具层的三类结局
        raise _translate_topology_error(exc) from exc

    rows = [{
        "from_service": getattr(edge, "from_service", None),
        "to_service": getattr(edge, "to_service", None),
        "call_type": getattr(edge, "call_type", "http"),
        "criticality": getattr(edge, "criticality", "medium"),
    } for edge in edges]
    return RawOutcome(
        data=rows,
        raw_ref=f"topology: {service_id} | direction={params.get('direction') or 'downstream'}"
                f" | depth={params.get('depth') or 1}",
        note=f"{len(rows)} 条依赖边",
    )


def _translate_topology_error(exc: Exception) -> Exception:
    """拓扑层的异常 → 工具层三类结局（**先分结局，再定重试策略**）。

    | 拓扑侧 | 工具侧 | 处置 |
    |---|---|---|
    | 连不上/没建表（`TopologyUnavailable`） | `SourceUnavailable` | **没查成** → 降级继续 |
    | 同名跨集群歧义（`TopologyAmbiguous`） | `QueryRejected` | **查询可修**（补集群名） → 让模型自修 |
    | 其它 | `SourceUnavailable` | 如实降级（不假装"没有依赖"） |
    """
    name = type(exc).__name__
    message = str(exc)
    if name == "TopologyAmbiguous":
        return QueryRejected(f"{message}（请补上 cluster 参数后重试）")
    if name == "TopologyUnavailable":
        return SourceUnavailable("TopologyUnavailable", message)
    return SourceUnavailable(name, f"拓扑查询失败（{name}）：{message}")


def _memory_slots(ctx: ToolContext, params: Mapping[str, Any]) -> Any:
    """`memory_read` 的 `slots`：把入参里的 service/cluster **装成 `Slots`**。

    ★ 为什么不能直接把 `service=` 传给 `recall`：它没有这个形参。
      而"对谁召回"这件事必须有地方承载 —— `recall(slots, symptom, top_k)` 的签名里
      只有 `slots` 能装，所以装进 `slots`。
    ★ 没给 service 时返回 `None`：那是"不按实体过滤"，不是"过滤一个空实体"。
    """
    from ..common.contracts import EntityRef, Slots

    service = str(params.get("service") or "").strip()
    if not service:
        return None
    cluster = str(params.get("cluster") or "").strip() or None
    # ★ `ToolContext` 里**没有** slots（只有数据源与黑板）—— 所以这里以最小 Slots 为底，
    #   只带上"对谁召回"这一个事实。别假装有请求上下文（那是我们没有的东西）。
    return Slots(env="prod", time_window="30m",
                 service=EntityRef.make("service", service, cluster))


async def _memory_read(ctx: ToolContext, params: Mapping[str, Any]) -> RawOutcome:
    """相似历史案例（记忆三路召回）。实现走 `MemoryService.recall`。"""
    if ctx.memory is None:
        raise SourceUnavailable("NotConfigured", "记忆服务未接入（MemoryService 尚未实现）")
    # ★★ 这里以前传的是 `service=`，而 `MemoryService.recall` 的形参是
    #   `(slots=None, symptom=None, *, top_k, tenant_id)` —— **没有 `service`**。
    #   传一个不存在的关键字参数，无论取值是什么都是 `TypeError`（不是"值为 None 就没事"），
    #   被执行器兜成 `INTERNAL_ERROR` → **记忆召回这一整条路径永远缺失**。
    #   当前之所以没暴露，只是因为 `agents/wiring.py` 还没注入 `memory`：
    #   先撞上上面那句 `SourceUnavailable` 提前返回 —— **接入的那一天就会当场碎**。
    #   → "对谁召回"由 `slots` 承载（`recall(slots, symptom, top_k)` 的签名里只有它）。
    from ..observed.context import current
    scope=current()
    hits = await ctx.memory.recall(
        _memory_slots(ctx, params),
        str(params.get("symptom") or ""),
        top_k=int(params.get("top_k") or 3),
        tenant_id=scope.namespace if scope else 'default',
    )
    rows = [hit if isinstance(hit, Mapping) else _as_mapping(hit) for hit in hits]
    return RawOutcome(
        data=rows,
        raw_ref=f"memory: symptom={str(params.get('symptom') or '')[:60]!r}"
                f" | service={params.get('service') or '*'}",
        note=f"{len(rows)} 条相似案例",
    )


def _as_mapping(obj: Any) -> dict[str, Any]:
    """把契约对象（如 `MemoryHit`）转成 dict（保留字段，不丢元数据）。"""
    if hasattr(obj, "model_dump"):
        return dict(obj.model_dump(mode="json"))
    if hasattr(obj, "__dict__"):
        return dict(vars(obj))
    return {"value": obj}


async def _evidence_lookup(ctx: ToolContext, params: Mapping[str, Any]) -> RawOutcome:
    """Critic 的**受限反查**：只能查黑板上**已经存在**的 evidence_id。

    ★ 两个"不"：不新增假设、不查原始遥测。
      查不到时返回 `data=[]`（**"查了，黑板上没有这条"**）而不是抛错 ——
      "证据不存在"本身是 Critic 需要知道的事实（可能正是别人引用了不存在的证据）。

    ★★ 这里曾经写成 `ctx.board.get_evidence(evidence_id)`（少一个参数），
      而 `BoardStore.get_evidence` 的签名是 `(incident_id, evidence_id)` ——
      于是这条"受限反查"**每次都以 TypeError 告终**（被执行器兜成 INTERNAL_ERROR），
      Critic 拿到的永远是"读不回来"，**任何引用证据的假设都会被排除**。
      → 现在：`ToolContext.incident_id` 由编排层注入；缺它就**如实说"没法反查"**
        （`SourceUnavailable` → 降级），而不是说成"证据不存在"。
    """
    from ..observed.context import current
    scope=current()
    if scope and ctx.incident_id not in scope.incident_ids:
        raise SourceUnavailable('ScopeDenied','当前黑板事故未绑定此系统；停止反查')
    if ctx.board is None:
        raise SourceUnavailable("NotConfigured", "黑板未接入（BoardStore 尚未实现）")
    if not ctx.incident_id:
        raise SourceUnavailable(
            "MissingIncident",
            "没带 incident_id → 无法反查黑板（黑板按 incident 组织）。"
            "★ 这不是「证据不存在」：是**没查成**")
    evidence_id = str(params["evidence_id"])
    record = await ctx.board.get_evidence(str(ctx.incident_id), evidence_id)
    rows = [] if record is None else [_as_mapping(record)]
    return RawOutcome(
        data=rows,
        raw_ref=f"board: incident={ctx.incident_id} evidence_id={evidence_id}",
        note="命中 1 条" if rows else "黑板上没有这条证据",
    )


def _stamp(fetched_at: float) -> str:
    """把抓取时刻写成带时区偏移的 ISO8601（**本地副本也要能对上时间轴**）。"""
    return datetime.fromtimestamp(float(fetched_at)).astimezone().isoformat(timespec="seconds")


def _text_of(data: Any) -> str:
    """原文的可读化：字符串原样，其余转 JSON（缩进只为读得下去）。"""
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _clip(text: str, limit: int) -> tuple[str, str]:
    """按字符裁剪，返回 `(文本, 裁剪留痕)` —— 砍了多少**必须**说出来。"""
    if len(text) <= limit:
        return text, ""
    return text[:limit], f"（超过 {limit} 字符已截断：原 {len(text)} 字符，砍掉 {len(text) - limit} 字符）"


def raw_ref_with_limits(raw_ref: str) -> str:
    """在结果 `raw_ref` 上补全**取数局限**与窗口长度（按它就一定能回取原文）。

    ★ 窗口长度从 `[...]` 里读回来现算：契约要求"更久远只在聚合里体现"这句话
      同时出现在 `raw_ref` 或 `note` 里（机器可查），而 `raw_ref` 又必须与
      落盘副本**逐字一致**（否则模型拷回来的引用找不到文件）。
    """
    if "取数局限" in raw_ref or "明细只召回最近" in raw_ref:
        return raw_ref
    matched = re.search(r"\[(\d+(?:\.\d+)?),(\d+(?:\.\d+)?)\]", raw_ref)
    window = (float(matched.group(2)) - float(matched.group(1))) if matched else 0.0
    extra = detail_truncation_note(window)
    # ★ 用 join 拼：`raw_ref` 本身可能已经以 `|` 结尾（失败路径的提示），
    #   直接字符串相加会留下一个空荡荡的尾巴。
    return " | ".join([raw_ref] + ([extra] if extra else []))


async def invoke_handler(handler: Any, ctx: ToolContext, params: Mapping[str, Any]) -> RawOutcome:
    """每次调用独立收集留痕，防止并发查询的窗口说明混入彼此结果。"""
    lines: list[str] = []
    try:
        with SinkScope() as lines:
            return persisted_raw(await handler(ctx, params))
    finally:
        for line in lines:
            note(line)


def persisted_raw(outcome: RawOutcome) -> RawOutcome:
    """**写入收口**：原始载荷在这里落盘，并补上"副本抓取于何时"的留痕。

    ★★ 为什么收成一处、由执行器在**摘要化之前**调用：每个 handler 各写一遍落盘，
      迟早有一个漏掉 —— 而漏掉的那个表现为"`raw_read` 说找不到"（看起来像工具
      坏了，其实是当初没存）。它**绝不影响取数**：写不进就只是这次取不回原文。
    ★★ 已经是**本地副本**的产物（`from_local_copy`）直接原样返回：
      `raw_read` 交回的 `data` 是**可能被 `max_chars` 裁过**的文本，而它的 `raw_ref`
      与落盘键逐字相同 —— 再落一次盘就是拿裁过的文本**覆盖原始载荷**（不可逆）。
    """
    from .rawstore import save_raw  # noqa: PLC0415 —— 与 rawstore 互相独立，用到才导

    if outcome.data is None or outcome.from_local_copy:
        return outcome
    ref = raw_ref_with_limits(outcome.raw_ref)
    try:
        save_raw(ref, outcome.data, fetched_at=time.time())
    except Exception as exc:              # noqa: BLE001 —— fail-open，只留痕
        logger.warning("原始数据落盘失败：%s", type(exc).__name__)
        note(f"原始数据落盘失败（{type(exc).__name__}）→ 本次取不回原文")
    return RawOutcome(data=outcome.data, raw_ref=ref,
                      note="；".join(part for part in (outcome.note, *collect()) if part))


async def _raw_read(ctx: ToolContext, params: Mapping[str, Any]) -> RawOutcome:
    """按引用取回**本地磁盘上的原始数据**（不重发上游请求）。"""
    raw_ref = str(params["raw_ref"]).strip()
    if raw_ref.startswith('attempt:'):
        from ..observed.attempt_read import raw
        return await raw(raw_ref,params)
    try:
        found = load_raw(raw_ref)
    except (OSError, ValueError) as exc:
        raise SourceUnavailable(
            type(exc).__name__,
            f"本地有 {raw_ref} 的副本，但读不出来（{type(exc).__name__}）—— "
            f"这是**读失败**，不是「没有这份数据」",
        ) from exc
    if found is None:
        raise SourceUnavailable(
            "NotFound",
            f"本地没有这份原始数据：{raw_ref}（可能已被淘汰或超过保留期）"
            f"—— 这是**找不到**，不是「查了没有数据」",
        )
    text, clipped = _clip(_text_of(found["data"]),
                          max(1, int(params.get("max_chars") or 20000)))
    return RawOutcome(
        data=text,
        raw_ref=raw_ref,
        note=f"本地副本，抓取于 {_stamp(found['fetched_at'])}{clipped}",
        # ★★ 这份 `data` 就是本地副本的内容（可能已被 `max_chars` 裁过）——
        #   标记它，写入收口才不会拿它把**原始载荷**覆盖掉（那是不可逆的丢失）。
        from_local_copy=True,
    )


async def _kb_search(ctx: ToolContext, params: Mapping[str, Any]) -> RawOutcome:
    """当前生效知识检索；未配置/失败与查询无命中严格区分。"""
    from ..knowledge.tools import kb_search
    return await kb_search(ctx,params)


async def _kb_document_read(ctx: ToolContext, params: Mapping[str, Any]) -> RawOutcome:
    from ..knowledge.tools import kb_document_read
    return await kb_document_read(ctx,params)


async def _change_write(ctx: ToolContext, params: Mapping[str, Any]) -> RawOutcome:
    """变更执行 —— 要等**沙箱 + 分级审批**建成后才提供。

    ★ 在写通道建成之前，这个工具必须**明确拒绝**：
      一个"看起来执行了"的假成功，比一个明确的拒绝危险得多。
    """
    raise ToolRefused(
        ErrorCode.TOOL_UNAVAILABLE.value, "NotImplemented",
        "变更写通道将在 **T21** 接入（诊断沙箱 + L0~L4 分级 + 人工审批）。"
        "本阶段**不会执行任何变更**；若确需处置，请人工操作",
    )


#: 工具名 → 实现（**登记表**：执行器只认这张表，不认别的入口）
from .math_tools import MATH_HANDLERS  # noqa: E402 —— 数据运算工具（纯本地，实现在那边）

HANDLERS: Final[dict[str, Any]] = {
    **MATH_HANDLERS,
    "promql_read": _promql_read,
    "logql_read": _logql_read,
    "traceql_read": _traceql_read,
    "trace_read": _trace_read,
    "change_read": _change_read,
    "topology_read": _topology_read,
    "memory_read": _memory_read,
    "evidence_lookup": _evidence_lookup,
    "raw_read": _raw_read,
    "kb_search": _kb_search,
    "kb_document_read": _kb_document_read,
    "change_write": _change_write,
}


def handler_for(tool: str) -> Any:
    """取工具实现。取不到 → 抛（**不存在"默认实现"**）。"""
    try:
        return HANDLERS[tool]
    except KeyError as exc:
        raise KeyError(f"未登记的工具实现：{tool!r}（可选：{sorted(HANDLERS)}）") from exc
